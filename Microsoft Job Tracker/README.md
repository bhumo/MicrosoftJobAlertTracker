# Microsoft Job Tracker

Monitor Microsoft Careers search results, detect changes in the top results, and email yourself when they change. The script renders the search page with Playwright (Chromium), parses job cards, and remembers the previous top results in SQLite to avoid duplicate alerts.

## Prerequisites
- Python 3.11+ (tested with 3.13)
- Gmail App Password (2FA-enabled account)
- macOS/Linux shell

## Setup
```bash
cd "Microsoft Job Tracker"
python3 -m venv .venv
source .venv/bin/activate
pip install requests beautifulsoup4 playwright
python -m playwright install chromium
```

## Run (common case)
```bash
cd "Microsoft Job Tracker"
source .venv/bin/activate
MS_KEYWORD="software engineer" \
MS_LOCATION="United States" \
GMAIL_USER="you@gmail.com" \
GMAIL_APP_PASS="your_app_password" \
EMAIL_TO="you@gmail.com" \
python microsft_live_Tracker.py
```
Stop anytime with `Ctrl+C`.

## Run with a custom search URL
If you already have the full careers URL (e.g., from apply.careers), override the search builder:
```bash
MS_SEARCH_URL="https://apply.careers.microsoft.com/careers?query=software+ic2&start=0&location=United+States&pid=1970393556643072&sort_by=timestamp&filter_include_remote=1" \
GMAIL_USER="you@gmail.com" \
GMAIL_APP_PASS="your_app_password" \
EMAIL_TO="you@gmail.com" \
python microsft_live_Tracker.py
```

## Configuration (env vars)
- `MS_KEYWORD` / `MS_LOCATION`: Search query and location (ignored if `MS_SEARCH_URL` is set).
- `MS_TOP_K` (default 5): How many top jobs to track in the email.
- `MS_POLL_MIN_SECONDS` / `MS_POLL_MAX_SECONDS` (defaults 120/180): Randomized wait between polls.
- `MS_COOLDOWN_SECONDS` (default 600): Minimum time between emails.
- `MS_DB_PATH` (default `ms_jobs_state.sqlite`): Where to store state.
- `MS_SEARCH_URL`: Full URL to use instead of the built search.
- `MS_SEARCH_BASE` (default Microsoft careers search): Base URL if you want to point elsewhere.

Email auth:
- `GMAIL_USER`: Sender address.
- `GMAIL_APP_PASS`: Gmail App Password (required).
- `EMAIL_FROM`: Optional override for “From” (defaults to `GMAIL_USER`).
- `EMAIL_TO`: Destination address (required).

## How it works
1) Renders the search page headlessly with Playwright/Chromium.  
2) Extracts job cards (`parse_jobs_from_html`) and builds a top-K snapshot.  
3) Compares to previous snapshot in SQLite; if changed and not in cooldown, sends an email summary.  
4) Logs each run and public IP for debugging.

## Customizing parsing
If zero jobs are parsed for a new layout, adjust selectors in `parse_jobs_from_html` (e.g., how job links, titles, locations, and posted dates are located). A quick sanity check:
```bash
python - <<'PY'
from microsft_live_Tracker import build_search_url, render_search_page, parse_jobs_from_html
html = render_search_page(build_search_url())
print("Jobs parsed:", len(parse_jobs_from_html(html)))
PY
```
If the count is 0, inspect the page HTML and refine the finders in that function.

## State and logs
- SQLite file: `ms_jobs_state.sqlite` (tables: `state`, `runs`, `ip_log`).
- The script prints status and errors to stdout; increase verbosity by adding your own prints as needed.

## Troubleshooting
- `Playwright is required...`: Ensure the `pip install` and `python -m playwright install chromium` steps were run inside the venv.
- `No jobs parsed from page`: Use `MS_SEARCH_URL` with a concrete search; then update `parse_jobs_from_html` selectors if the layout differs.
- Email not sending: Confirm `GMAIL_USER`, `GMAIL_APP_PASS`, and `EMAIL_TO` are set; check for cooldown (`MS_COOLDOWN_SECONDS`).
