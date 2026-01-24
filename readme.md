# All Direct Flights

`scripts/flightsfrom_scrape.py` scrapes direct-route metadata from [flightsfrom.com](https://www.flightsfrom.com/) and emits one CSV per origin airport. The scraper renders the site with Playwright (Chromium), parses the dynamically generated route tiles, and normalizes the data into a consistently ordered CSV.

## Requirements

- Python 3.9 or later.
- Install dependencies from `requirements.txt` so Playwright, BeautifulSoup and lxml are available.
- Install the Playwright browsers after the pip step:

  ```sh
  python3 -m playwright install
  ```

## Installation

```sh
python3 -m pip install -r requirements.txt
python3 -m playwright install
```

If you already have Playwright installed but the browsers are missing, rerun the `playwright install` step to fetch Chromium/Firefox/WebKit binaries.

## Usage

```sh
python3 scripts/flightsfrom_scrape.py --airports=WAW,KRK --out-dir=out
```

- `--airports` (required) accepts a comma-separated list of IATA codes. The script uppercases and trims each entry.
- `--out-dir` (optional) defaults to `out`.
- Each airport produces `out/<IATA>.csv`, and status logs are written to standard error so you can pipe or wrap the command without losing progress.

If any airport fails, a corresponding `out/<IATA>.error.txt` is created and the process exits with status code `2`.

## Output format

Each CSV uses the following columns (in order):

1. `origin_iata`
2. `destination_iata`
3. `destination_country_iso2`
4. `airline_name`
5. `airline_iata`
6. `flights_per_day_min`
7. `flights_per_day_max`
8. `duration_minutes`
9. `route_url`
10. `scraped_at` (UTC ISO timestamp)
11. `flights_per_day_raw`
12. `duration_raw`
13. `operating_days` (comma-separated numeric ranges for Mon=1 .. Sun=7, e.g. `1-5` or `1-7`)
14. `blocked_days` (same numeric encoding for days without scheduled flights; blank when the route serves every day)
15. `airline_logo_url`

The rows are sorted by destination IATA, then airline code/name, and the scraper deduplicates on `(origin, destination, airline)` to keep the latest view of each pair.

## Reliability notes

- The scraper opens a fresh Playwright browser context per airport to avoid stale cookies or Cloudflare puzzles induced by repeated navigation in one tab.
- It also detects the “Just a moment…” (Cloudflare) interstitial and pauses briefly before retrying the selector so the scrape can resume once the challenge clears.
- The new `operating_days`/`blocked_days` columns reflect the day-of-week badges on flightsfrom.com so you can filter routes by weekdays (with exact Sun–Sat labels).
- Slow networks or raised blockers can still trigger timeouts (`Page.wait_for_selector`), and the script will log the stack trace into `*.error.txt` so you can inspect it.

## Troubleshooting

- If you see `Page.wait_for_selector: Timeout 25000ms exceeded` for a second airport, re-run just that airport to verify the site is reachable.
- Confirm the browsers are installed: `python3 -m playwright install`.
- If you need to capture a different locale or user agent (to mimic another region), adjust the constants in `scripts/flightsfrom_scrape.py`.

## Tests

- `pytest tests/integration/test_direct_weekly.py` drives Playwright against `flightsfrom.com` for WAW, GDN and KRK and ensures the selected direct destinations (WAW→FRA/AMS/GDN/KRK/CPH, GDN→LTN/STN/WAW/CPH, KRK→STN/AMS/WAW) advertise every weekday in `operating_days`.
