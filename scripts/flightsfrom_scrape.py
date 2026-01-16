from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

WAIT_MS = 25000


@dataclass
class Row:
    origin_iata: str
    destination_iata: str
    destination_city: str
    destination_country_iso2: str
    destination_airport_name: str

    airline_name: str
    airline_iata: str

    flights_per_day_min: Optional[int]
    flights_per_day_max: Optional[int]
    flights_per_day_raw: str

    duration_minutes: Optional[int]
    duration_raw: str

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

    m = re.search(r"(\d+)\s+flight\s+per\s+day", t)
    if m:
        x = int(m.group(1))
        return x, x

    m = re.search(r"(\d+)\s+flights\s+per\s+day", t)
    if m:
        x = int(m.group(1))
        return x, x

    return None, None


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


def extract_country_and_airport_from_flag(wrapper: BeautifulSoup) -> Tuple[str, str]:
    img = wrapper.select_one("img.flag-image[uk-tooltip]")
    if not img:
        return "", ""
    tip = (img.get("uk-tooltip") or "").strip()
    m = re.match(r"^([A-Z]{2})\s*-\s*(.+)$", tip)
    if not m:
        return "", ""
    return m.group(1), m.group(2).strip()


def fetch_rendered_html(page, airport_iata: str) -> str:
    url = f"https://www.flightsfrom.com/{airport_iata}"
    page.goto(url, wait_until="domcontentloaded")

    # czekamy aż lista tras się pojawi
    page.wait_for_selector(".ff-wrapper", timeout=WAIT_MS)
    page.wait_for_selector(f'a[href^="/{airport_iata}-"]', timeout=WAIT_MS)

    # scroll — na wypadek lazy-load
    for _ in range(7):
        page.mouse.wheel(0, 8000)
        page.wait_for_timeout(600)

    return page.content()


def parse_rows(html: str, origin: str) -> List[Row]:
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
        city = strong.get_text(strip=True) if strong else ""

        dest_country_iso2, dest_airport_name = extract_country_and_airport_from_flag(wrapper)

        airline_img = wrapper.select_one("div.ff-row-airline img.ff-image-airline")
        airline_name = (airline_img.get("alt") or "").strip() if airline_img else ""
        airline_logo_url = (airline_img.get("src") or "").strip() if airline_img else ""
        airline_iata = extract_airline_iata_from_logo(airline_logo_url)

        fpd_el = wrapper.select_one(".ff-flights-daily, .ff-flights-daily-desktop")
        flights_per_day_raw = fpd_el.get_text(" ", strip=True) if fpd_el else ""
        fpd_min, fpd_max = parse_flights_per_day(flights_per_day_raw)

        dur_el = wrapper.select_one(".ff-row-durationnr, .ff-row-text-durationnr, .ff-row-duration span")
        duration_raw = dur_el.get_text(" ", strip=True) if dur_el else ""
        duration_minutes = parse_duration_minutes(duration_raw)

        rows.append(
            Row(
                origin_iata=origin,
                destination_iata=dest_iata,
                destination_city=city,
                destination_country_iso2=dest_country_iso2,
                destination_airport_name=dest_airport_name,
                airline_name=airline_name,
                airline_iata=airline_iata,
                flights_per_day_min=fpd_min,
                flights_per_day_max=fpd_max,
                flights_per_day_raw=flights_per_day_raw,
                duration_minutes=duration_minutes,
                duration_raw=duration_raw,
                airline_logo_url=airline_logo_url,
                route_url="https://www.flightsfrom.com" + href,
                scraped_at=scraped_at,
            )
        )

    # dedupe (origin, dest, airline)
    uniq = {}
    for r in rows:
        key = (r.origin_iata, r.destination_iata, r.airline_iata or r.airline_name)
        uniq[key] = r

    return list(uniq.values())


def rows_to_csv(rows: List[Row]) -> str:
    out = io.StringIO()
    fieldnames = [
        "origin_iata",
        "destination_iata",
        "destination_city",
        "destination_country_iso2",
        "destination_airport_name",
        "airline_name",
        "airline_iata",
        "flights_per_day_min",
        "flights_per_day_max",
        "duration_minutes",
        "route_url",
        "scraped_at",
        "flights_per_day_raw",
        "duration_raw",
        "airline_logo_url",
    ]
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    for r in sorted(rows, key=lambda x: (x.destination_iata, x.airline_iata, x.airline_name)):
        w.writerow(
            {
                "origin_iata": r.origin_iata,
                "destination_iata": r.destination_iata,
                "destination_city": r.destination_city,
                "destination_country_iso2": r.destination_country_iso2,
                "destination_airport_name": r.destination_airport_name,
                "airline_name": r.airline_name,
                "airline_iata": r.airline_iata,
                "flights_per_day_min": "" if r.flights_per_day_min is None else r.flights_per_day_min,
                "flights_per_day_max": "" if r.flights_per_day_max is None else r.flights_per_day_max,
                "duration_minutes": "" if r.duration_minutes is None else r.duration_minutes,
                "route_url": r.route_url,
                "scraped_at": r.scraped_at,
                "flights_per_day_raw": r.flights_per_day_raw,
                "duration_raw": r.duration_raw,
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
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()

        for airport in airports:
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

        ctx.close()
        browser.close()

    # jeśli są error.txt, zwróć 2
    if any(out_dir.glob("*.error.txt")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
