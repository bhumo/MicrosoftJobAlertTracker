import os, time, random, json, hashlib, sqlite3, smtplib, re
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# =======================
# CONFIG
# =======================
KEYWORD = os.getenv("MS_KEYWORD", "software engineer")
LOCATION = os.getenv("MS_LOCATION", "United States")
TOP_K = int(os.getenv("MS_TOP_K", "5"))

POLL_MIN_SECONDS = int(os.getenv("MS_POLL_MIN_SECONDS", "120"))
POLL_MAX_SECONDS = int(os.getenv("MS_POLL_MAX_SECONDS", "180"))

COOLDOWN_SECONDS = int(os.getenv("MS_COOLDOWN_SECONDS", str(10 * 60)))  # don't email more than once per 10 minutes
DB_PATH = os.getenv("MS_DB_PATH", "ms_jobs_state.sqlite")

SEARCH_BASE = os.getenv("MS_SEARCH_BASE", "https://jobs.careers.microsoft.com/global/en/search")
CUSTOM_SEARCH_URL = os.getenv("MS_SEARCH_URL")  # allow overriding with a full URL like the apply.careers.microsoft.com query
HEADERS = {
    "User-Agent": "Mozilla/5.0 (MSJobsMonitor/2.0; +https://example.com)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Referer": "https://careers.microsoft.com/",
}

# Gmail SMTP via App Password
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.getenv("GMAIL_USER", "")       # your@gmail.com
SMTP_PASS = os.getenv("GMAIL_APP_PASS", "")   # app password
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO", "")          # destination email

# =======================
# DB
# =======================
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS state(
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS runs(
        ts_utc TEXT NOT NULL,
        signature TEXT NOT NULL,
        top5_json TEXT NOT NULL
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS ip_log(
        ts_utc TEXT NOT NULL,
        ip TEXT NOT NULL
      )
    """)
    conn.commit()
    return conn

def state_get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    return row[0] if row else None

def state_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    conn.commit()

# =======================
# Helpers
# =======================
def parse_dt(job: Dict[str, Any]) -> Optional[datetime]:
    for key in ("postingDate", "postedDate", "startDate", "datePosted"):
        v = job.get(key)
        if not v:
            continue
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass
        if isinstance(v, (int, float)):
            try:
                if v > 10_000_000_000:  # ms
                    return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
                return datetime.fromtimestamp(v, tz=timezone.utc)
            except Exception:
                pass
    # Handle relative text like "2 days ago"
    rel = job.get("posted_text")
    if isinstance(rel, str):
        m = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", rel.lower())
        if m:
            qty = int(m.group(1))
            unit = m.group(2)
            delta = {
                "minute": qty * 60,
                "hour": qty * 3600,
                "day": qty * 86400,
                "week": qty * 604800,
                "month": qty * 2629746,  # average month in seconds
            }[unit]
            return datetime.now(timezone.utc) - timedelta(seconds=delta)
    return None

def job_id(job: Dict[str, Any]) -> str:
    for k in ("jobId", "id", "reqId", "requisitionId"):
        if job.get(k) is not None:
            return str(job[k])
    return f"fallback::{job.get('title','')}::{job.get('postingDate','')}"

def title(job: Dict[str, Any]) -> str:
    return str(job.get("title") or job.get("name") or "Untitled")

def loc(job: Dict[str, Any]) -> str:
    return str(job.get("location") or job.get("primaryLocation") or "N/A")

def url(job: Dict[str, Any]) -> str:
    for k in ("jobUrl", "url", "postingUrl"):
        if job.get(k):
            return str(job[k])
    return f"https://apply.careers.microsoft.com/careers/job/{job_id(job)}"

def build_search_url() -> str:
    if CUSTOM_SEARCH_URL:
        return CUSTOM_SEARCH_URL
    q = quote_plus(KEYWORD)
    loc_q = quote_plus(LOCATION)
    # The site sorts client-side, so we request most recent and let DOM order be our recency signal.
    return f"{SEARCH_BASE}?keywords={q}&location={loc_q}&sortBy=DT_DESC"

def render_search_page(url: str) -> str:
    """Render the jobs page (JS required) and return the HTML."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for HTML rendering. Install with `pip install playwright` and run `playwright install chromium`.") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        # Some Microsoft pages never reach true network idle; fall back to 'load' with a larger timeout.
        page.goto(url, wait_until="load", timeout=60_000)
        # Try to wait for job links to render; ignore failures to keep going.
        try:
            page.wait_for_selector("a[href*='/job/']", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(2_000)  # let lazy-loaded cards paint
        html = page.content()
        browser.close()
    return html

def parse_jobs_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: List[Dict[str, Any]] = []
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/job/" not in href:
            continue
        jobid_match = re.search(r"/job/([0-9]+)/", href)
        jid = jobid_match.group(1) if jobid_match else href
        if jid in seen_ids:
            continue

        title_text = a.get_text(" ", strip=True) or "Untitled"
        card = a.find_parent(["article", "li", "div"])
        location_text = None
        posted_text = None

        if card:
            # look for common location / date markers inside the card
            loc_el = card.find(lambda tag: tag.name in ("span", "div") and "location" in " ".join(tag.get("class", [])).lower())
            if loc_el and loc_el.get_text(strip=True):
                location_text = loc_el.get_text(" ", strip=True)

            date_el = card.find(lambda tag: tag.name in ("span", "div") and ("posted" in " ".join(tag.get("class", [])).lower() or "date" in " ".join(tag.get("class", [])).lower()))
            if date_el and date_el.get_text(strip=True):
                posted_text = date_el.get_text(" ", strip=True)

        job_url = urljoin("https://jobs.careers.microsoft.com", href)
        jobs.append({
            "id": jid,
            "title": title_text,
            "location": location_text or "N/A",
            "posted_text": posted_text,
            "url": job_url,
        })
        seen_ids.add(jid)

    return jobs

def fetch_jobs() -> List[Dict[str, Any]]:
    search_url = build_search_url()
    html = render_search_page(search_url)
    jobs = parse_jobs_from_html(html)
    if not jobs:
        raise RuntimeError("No jobs parsed from page. Check selectors or ensure page renders (Playwright installed).")
    return jobs

def top5_snapshot(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for j in jobs[:TOP_K]:
        dt = parse_dt(j)
        out.append({
            "id": job_id(j),
            "title": title(j),
            "location": loc(j),
            "posted_utc": dt.isoformat() if dt else None,
            "posted_text": j.get("posted_text"),
            "url": url(j),
        })
    return out

def signature(top5: List[Dict[str, Any]]) -> str:
    # Order-sensitive signature
    payload = json.dumps([(x["id"], x["title"]) for x in top5], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def diff(prev: List[Dict[str, Any]], curr: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    prev_ids = {x["id"] for x in prev}
    curr_ids = {x["id"] for x in curr}
    entered = [x["title"] for x in curr if x["id"] not in prev_ids]
    left = [x["title"] for x in prev if x["id"] not in curr_ids]
    return entered, left

def log_public_ip(conn) -> None:
    """Capture and store the current public IP so you can spot rotations / blocks."""
    try:
        ip_resp = requests.get("https://api.ipify.org", timeout=8)
        ip_resp.raise_for_status()
        ip = ip_resp.text.strip()
        conn.execute(
            "INSERT INTO ip_log(ts_utc, ip) VALUES(?,?)",
            (datetime.now(timezone.utc).isoformat(), ip),
        )
        conn.commit()
    except Exception:
        # Don't crash the monitor if IP logging fails (e.g., transient DNS)
        pass

# =======================
# Email
# =======================
def send_email(subject: str, body: str) -> None:
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        raise RuntimeError("Set env vars: GMAIL_USER, GMAIL_APP_PASS, EMAIL_TO (and optionally EMAIL_FROM).")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

def format_body(prev: Optional[List[Dict[str, Any]]], curr: List[Dict[str, Any]]) -> str:
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"Microsoft Careers Top {TOP_K} changed",
        f"keyword: {KEYWORD}",
        f"location: {LOCATION}",
        f"time (UTC): {now}",
        "",
    ]
    if prev:
        entered, left = diff(prev, curr)
        if entered:
            lines.append("Entered Top 5:")
            for t in entered: lines.append(f"  + {t}")
            lines.append("")
        if left:
            lines.append("Left Top 5:")
            for t in left: lines.append(f"  - {t}")
            lines.append("")

    lines.append("New Top 5:")
    for i, j in enumerate(curr, start=1):
        lines.append(f"{i}. {j['title']} — {j['location']}")
        posted = j.get("posted_utc") or j.get("posted_text")
        lines.append(f"   Posted: {posted}")
        lines.append(f"   {j['url']}")
    return "\n".join(lines)

def validate_config() -> None:
    if POLL_MIN_SECONDS <= 0 or POLL_MAX_SECONDS <= 0:
        raise ValueError("Polling interval must be positive")
    if POLL_MIN_SECONDS > POLL_MAX_SECONDS:
        raise ValueError("MS_POLL_MIN_SECONDS cannot exceed MS_POLL_MAX_SECONDS")
    if TOP_K <= 0:
        raise ValueError("MS_TOP_K must be >= 1")

# =======================
# Main loop
# =======================
def main():
    validate_config()
    conn = db_conn()
    print("MS Jobs monitor running… (Ctrl+C to stop)")
    i=0
    while i<2:
        try:
            log_public_ip(conn)
            jobs = fetch_jobs()
            if len(jobs) < TOP_K:
                print(f"[{datetime.now().isoformat()}] only {len(jobs)} results, skipping")
            else:
                curr_top5 = top5_snapshot(jobs)
                curr_sig = signature(curr_top5)

                prev_sig = state_get(conn, "top5_sig")
                prev_top5_json = state_get(conn, "top5_json")
                prev_top5 = json.loads(prev_top5_json) if prev_top5_json else None

                # Save run history for debugging
                conn.execute(
                    "INSERT INTO runs(ts_utc, signature, top5_json) VALUES(?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), curr_sig, json.dumps(curr_top5))
                )
                conn.commit()

                if prev_sig != curr_sig:
                    # cooldown guard
                    last_sent = state_get(conn, "last_sent_utc")
                    can_send = True
                    if last_sent:
                        last_dt = datetime.fromisoformat(last_sent)
                        if (datetime.now(timezone.utc) - last_dt).total_seconds() < COOLDOWN_SECONDS:
                            can_send = False

                    if can_send:
                        subject = f"[Microsoft Alert] Top {TOP_K} changed: {KEYWORD} ({LOCATION})"
                        body = format_body(prev_top5, curr_top5)
                        send_email(subject, body)
                        state_set(conn, "last_sent_utc", datetime.now(timezone.utc).isoformat())
                        print(f"[{datetime.now().isoformat()}] change detected → email sent")
                    else:
                        print(f"[{datetime.now().isoformat()}] change detected but in cooldown")

                    # Always update state
                    state_set(conn, "top5_sig", curr_sig)
                    state_set(conn, "top5_json", json.dumps(curr_top5))
                else:
                    print(f"[{datetime.now().isoformat()}] no change")

        except Exception as e:
            print(f"[{datetime.now().isoformat()}] ERROR: {e}")
        print(" {i+1} round complet",i)
        i+=1
        time.sleep(random.randint(POLL_MIN_SECONDS, POLL_MAX_SECONDS))

if __name__ == "__main__":
    main()
