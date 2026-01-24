from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

WAIT_MS = 25000
CLOUDFLARE_RETRIES = 6
CLOUDFLARE_WAIT_MS = 2000
WEEKDAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
WEEKDAY_FROM_DAY_FLAGS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_TO_NUMBER = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6, "Sun": 7}


def is_cloudflare_challenge(page) -> bool:
    title = (page.title() or "").strip().lower()
    url = (page.url or "").lower()
    if "just a moment" in title or "checking your browser" in title:
        return True
    return "/cdn-cgi/" in url


def wait_for_route_list(page, airport: str) -> None:
    attempts = 0
    last_exc: Optional[PlaywrightTimeoutError] = None
    while attempts < CLOUDFLARE_RETRIES:
        try:
            page.wait_for_selector(".ff-wrapper", timeout=WAIT_MS)
            page.wait_for_selector(f'a[href^="/{airport}-"]', timeout=WAIT_MS)
            return
        except PlaywrightTimeoutError as exc:
            last_exc = exc
            if not is_cloudflare_challenge(page):
                raise
            print(
                f"[WARN] {airport}: Cloudflare challenge detected, retrying...",
                file=sys.stderr,
            )
            attempts += 1
            page.wait_for_timeout(CLOUDFLARE_WAIT_MS)
    if last_exc:
        raise last_exc
    raise PlaywrightTimeoutError(f"timeout waiting for routes for {airport}")


def expand_show_more_routes(page) -> None:
    for _ in range(5):
        try:
            button = page.wait_for_selector("#show-more-routes", timeout=2000)
        except PlaywrightTimeoutError:
            return
        if not button.is_visible():
            return
        try:
            button.click()
        except PlaywrightTimeoutError:
            return
        page.wait_for_timeout(1200)


def scroll_until_routes_loaded(page) -> None:
    last_count = 0
    stable_rounds = 0
    for _ in range(18):
        count = page.evaluate("() => document.querySelectorAll('div.ff-wrapper').length")
        if count > last_count:
            last_count = count
            stable_rounds = 0
        else:
            stable_rounds += 1
        page.mouse.wheel(0, 8000)
        page.wait_for_timeout(700)
        if stable_rounds >= 3:
            break


def compress_day_numbers(days: List[int]) -> str:
    if not days:
        return ""
    days = sorted(set(days))
    ranges = []
    start = prev = days[0]
    for day in days[1:]:
        if day == prev + 1:
            prev = day
            continue
        ranges.append((start, prev))
        start = prev = day
    ranges.append((start, prev))
    parts = []
    for start, end in ranges:
        parts.append(f"{start}-{end}" if start != end else f"{start}")
    return ",".join(parts)


def parse_weekday_availability(wrapper: BeautifulSoup) -> Tuple[str, str]:
    days_active = []
    days_blocked = []
    els = wrapper.select(".flightsfrom-list-days")
    for idx, day_el in enumerate(els[: len(WEEKDAY_SHORT)]):
        if not day_el:
            continue
        text = (day_el.get_text(strip=True) or "").strip()
        if not text:
            continue
        weekday = WEEKDAY_SHORT[idx]
        disabled = False
        classes = day_el.get("class", [])
        if isinstance(classes, str):
            disabled = "disabled" in classes.split()
        else:
            disabled = "disabled" in classes
        num = WEEKDAY_TO_NUMBER.get(weekday)
        if num is None:
            continue
        (days_blocked if disabled else days_active).append(num)

    return compress_day_numbers(days_active), compress_day_numbers(days_blocked)


@dataclass
class Row:
    origin_iata: str
    destination_iata: str
    destination_country_iso2: str

    airline_name: str
    airline_iata: str

    flights_per_day_min: Optional[int]
    flights_per_day_max: Optional[int]
    flights_per_day_raw: str

    duration_minutes: Optional[int]
    duration_raw: str

    operating_days: str
    blocked_days: str

    airline_logo_url: str
    route_url: str
    scraped_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_flights_per_day(text: str) -> Tuple[Optional[int], Optional[int]]:
    t = (text or "").strip().lower()
    if not t:
        return None, None

    m = re.search(r"(\d+)\s*-\s*(\d+)\s+flights?\s+per\s+day", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d+)\s*-\s*(\d+)\s+flights?\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"^(\d+)\s*-\s*(\d+)$", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d+)\s+flight\s+per\s+day", t)
    if m:
        x = int(m.group(1))
        return x, x

    m = re.search(r"(\d+)\s+flights\s+per\s+day", t)
    if m:
        x = int(m.group(1))
        return x, x

    m = re.search(r"(\d+)\s+flight\b", t)
    if m:
        x = int(m.group(1))
        return x, x

    m = re.search(r"(\d+)\s+flights\b", t)
    if m:
        x = int(m.group(1))
        return x, x

    m = re.search(r"^(\d+)$", t)
    if m:
        x = int(m.group(1))
        return x, x

    return None, None


def normalize_flights_per_day_raw(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return ""
    m = re.search(r"(\d+)\s*-\s*(\d+)\s+flights?\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"(\d+)\s+flights?\b", t)
    if m:
        return m.group(1)
    return text.strip()


def parse_duration_minutes(text: str) -> Optional[int]:
    t = (text or "").strip().lower()
    if not t:
        return None

    hours = 0
    mins = 0

    mh = re.search(r"(\d+)\s*h", t)
    if mh:
        hours = int(mh.group(1))

    mm = re.search(r"(\d+)\s*m", t)
    if mm:
        mins = int(mm.group(1))

    total = hours * 60 + mins
    return total if total > 0 else None


def extract_airline_iata_from_logo(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"/([A-Z0-9]{2})_100px\.png", url)
    return m.group(1) if m else ""


def normalize_path(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        parts = url.split("://", 1)[-1].split("/", 1)
        if len(parts) == 2:
            return parts[1]
        return ""
    return url.lstrip("/")


def extract_country_and_airport_from_flag(wrapper: BeautifulSoup) -> Tuple[str, str]:
    img = wrapper.select_one("img.flag-image[uk-tooltip]")
    if not img:
        return "", ""
    tip = (img.get("uk-tooltip") or "").strip()
    m = re.match(r"^([A-Z]{2})\s*-\s*(.+)$", tip)
    if not m:
        return "", ""
    return m.group(1), m.group(2).strip()


def extract_all_destinations(html: str) -> Optional[list]:
    marker = "allDestinations:"
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("[", idx)
    if start == -1:
        return None
    in_str = False
    esc = False
    depth = 0
    end = None
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end is None:
        return None
    raw = html[start:end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def parse_weekdays_from_flags(dest: dict) -> Tuple[str, str]:
    days_active = []
    days_blocked = []
    for idx, name in enumerate(WEEKDAY_FROM_DAY_FLAGS, start=1):
        flag = dest.get(f"day{idx}")
        if flag in ("yes", "upcoming"):
            days_active.append(WEEKDAY_TO_NUMBER[name])
        else:
            days_blocked.append(WEEKDAY_TO_NUMBER[name])
    return compress_day_numbers(days_active), compress_day_numbers(days_blocked)


def rows_from_all_destinations(data: list, origin: str) -> List[Row]:
    scraped_at = now_iso()
    rows: List[Row] = []
    for dest in data:
        if dest.get("iata_from") != origin:
            continue
        airport = dest.get("airport") or {}
        dest_iata = dest.get("iata_to") or ""
        country_iso2 = airport.get("country_code") or ""
        airport_name = airport.get("name") or ""
        flights_raw = normalize_flights_per_day_raw(dest.get("flights_per_day") or "")
        fpd_min, fpd_max = parse_flights_per_day(flights_raw)
        duration_minutes = dest.get("common_duration") or None
        duration_raw = f"{duration_minutes}m" if duration_minutes else ""
        operating_days, blocked_days = parse_weekdays_from_flags(dest)

        airlineroutes = dest.get("airlineroutes") or []
        if not airlineroutes:
            rows.append(
                Row(
                    origin_iata=origin,
                    destination_iata=dest_iata,
                    destination_country_iso2=country_iso2,
                    airline_name="",
                    airline_iata="",
                    flights_per_day_min=fpd_min,
                    flights_per_day_max=fpd_max,
                    flights_per_day_raw=flights_raw,
                    duration_minutes=duration_minutes,
                    duration_raw=duration_raw,
                    operating_days=operating_days,
                    blocked_days=blocked_days,
                    airline_logo_url="",
                    route_url=f"https://www.flightsfrom.com/{origin}-{dest_iata}",
                    scraped_at=scraped_at,
                )
            )
            continue

        for route in airlineroutes:
            airline = route.get("airline") or {}
            airline_iata = airline.get("IATA") or route.get("carrier") or ""
            airline_name = airline.get("name") or route.get("carrier_name") or ""
            airline_logo_url = (
                f"airlines/100/{airline_iata}_100px.png" if airline_iata else ""
            )

            rows.append(
                Row(
                    origin_iata=origin,
                    destination_iata=dest_iata,
                    destination_country_iso2=country_iso2,
                    airline_name=airline_name,
                    airline_iata=airline_iata,
                    flights_per_day_min=fpd_min,
                    flights_per_day_max=fpd_max,
                    flights_per_day_raw=flights_raw,
                    duration_minutes=duration_minutes,
                    duration_raw=duration_raw,
                    operating_days=operating_days,
                    blocked_days=blocked_days,
                    airline_logo_url=airline_logo_url,
                    route_url=f"{origin}-{dest_iata}",
                    scraped_at=scraped_at,
                )
            )

    return rows


def fetch_rendered_html(page, airport_iata: str) -> str:
    url = f"https://www.flightsfrom.com/{airport_iata}"
    page.goto(url, wait_until="domcontentloaded")

    wait_for_route_list(page, airport_iata)

    # scroll — na wypadek lazy-load
    scroll_until_routes_loaded(page)

    expand_show_more_routes(page)

    return page.content()


def parse_rows(html: str, origin: str) -> List[Row]:
    data = extract_all_destinations(html)
    if data:
        rows = rows_from_all_destinations(data, origin)
        return dedupe_rows(rows)

    soup = BeautifulSoup(html, "lxml")
    scraped_at = now_iso()
    rows: List[Row] = []

    for wrapper in soup.select("div.ff-wrapper"):
        a = wrapper.select_one(f'div.ff-row-name a[href^="/{origin}-"]')
        if not a:
            continue

        href = (a.get("href") or "").strip()
        m = re.match(rf"^/{origin}-([A-Z0-9]{{3}})$", href)
        if not m:
            continue
        dest_iata = m.group(1)

        strong = a.select_one("strong")
        dest_country_iso2, _dest_airport_name = extract_country_and_airport_from_flag(wrapper)

        airline_img = wrapper.select_one("div.ff-row-airline img.ff-image-airline")
        airline_name = (airline_img.get("alt") or "").strip() if airline_img else ""
        airline_logo_url = normalize_path(
            (airline_img.get("src") or "").strip() if airline_img else ""
        )
        airline_iata = extract_airline_iata_from_logo(airline_logo_url)

        fpd_el = wrapper.select_one(".ff-flights-daily, .ff-flights-daily-desktop")
        flights_per_day_raw = normalize_flights_per_day_raw(
            fpd_el.get_text(" ", strip=True) if fpd_el else ""
        )
        fpd_min, fpd_max = parse_flights_per_day(flights_per_day_raw)

        dur_el = wrapper.select_one(".ff-row-durationnr, .ff-row-text-durationnr, .ff-row-duration span")
        duration_raw = dur_el.get_text(" ", strip=True) if dur_el else ""
        duration_minutes = parse_duration_minutes(duration_raw)
        operating_days, blocked_days = parse_weekday_availability(wrapper)

        rows.append(
            Row(
                origin_iata=origin,
                destination_iata=dest_iata,
                destination_country_iso2=dest_country_iso2,
                airline_name=airline_name,
                airline_iata=airline_iata,
                flights_per_day_min=fpd_min,
                flights_per_day_max=fpd_max,
                flights_per_day_raw=flights_per_day_raw,
                duration_minutes=duration_minutes,
                duration_raw=duration_raw,
                operating_days=operating_days,
                blocked_days=blocked_days,
                airline_logo_url=airline_logo_url,
                route_url=f"{origin}-{dest_iata}",
                scraped_at=scraped_at,
            )
        )

    return dedupe_rows(rows)


def dedupe_rows(rows: List[Row]) -> List[Row]:
    # dedupe (origin, dest, airline)
    uniq = {}
    for r in rows:
        key = (r.origin_iata, r.destination_iata, r.airline_iata or r.airline_name)
        uniq[key] = r
    return list(uniq.values())


def setup_request_blocking(ctx) -> None:
    def handle_route(route, request):
        if request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()

    ctx.route("**/*", handle_route)


def rows_to_csv(rows: List[Row]) -> str:
    out = io.StringIO()
    fieldnames = [
        "origin_iata",
        "destination_iata",
        "destination_country_iso2",
        "airline_name",
        "airline_iata",
        "flights_per_day_min",
        "flights_per_day_max",
        "duration_minutes",
        "route_url",
        "scraped_at",
        "flights_per_day_raw",
        "duration_raw",
        "operating_days",
        "blocked_days",
        "airline_logo_url",
    ]
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    for r in sorted(rows, key=lambda x: (x.destination_iata, x.airline_iata, x.airline_name)):
        w.writerow(
            {
                "origin_iata": r.origin_iata,
                "destination_iata": r.destination_iata,
                "destination_country_iso2": r.destination_country_iso2,
                "airline_name": r.airline_name,
                "airline_iata": r.airline_iata,
                "flights_per_day_min": "" if r.flights_per_day_min is None else r.flights_per_day_min,
                "flights_per_day_max": "" if r.flights_per_day_max is None else r.flights_per_day_max,
                "duration_minutes": "" if r.duration_minutes is None else r.duration_minutes,
                "route_url": r.route_url,
                "scraped_at": r.scraped_at,
                "flights_per_day_raw": r.flights_per_day_raw,
                "duration_raw": r.duration_raw,
                "operating_days": r.operating_days,
                "blocked_days": r.blocked_days,
                "airline_logo_url": r.airline_logo_url,
            }
        )
    return out.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--airports",
        required=True,
        help="Comma-separated IATA list, e.g. GDN,WAW,KRK",
    )
    ap.add_argument(
        "--out-dir",
        default="out",
        help="Output directory for CSVs (default: out)",
    )
    args = ap.parse_args()

    airports = [a.strip().upper() for a in args.airports.split(",") if a.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Playwright: jedna przeglądarka, jedna strona, wiele lotnisk (szybciej i stabilniej)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for airport in airports:
            ctx = browser.new_context(user_agent=UA, locale="en-US")
            setup_request_blocking(ctx)
            page = ctx.new_page()
            try:
                html = fetch_rendered_html(page, airport)
                rows = parse_rows(html, airport)
                csv_text = rows_to_csv(rows)
                out_path = out_dir / f"{airport}.csv"
                out_path.write_text(csv_text, encoding="utf-8")
                print(f"[OK] {airport}: rows={len(rows)} file={out_path}", file=sys.stderr)
            except Exception as e:
                print(f"[ERROR] {airport}: {e}", file=sys.stderr)
                # Nie wywracaj całej paczki, ale sygnalizuj błąd kodem wyjścia
                # (na końcu zsumujemy statusy)
                (out_dir / f"{airport}.error.txt").write_text(str(e), encoding="utf-8")
            finally:
                page.close()
                ctx.close()

        browser.close()

    # jeśli są error.txt, zwróć 2
    if any(out_dir.glob("*.error.txt")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
