import re
from typing import Dict, Optional, Set

import pytest
from playwright.sync_api import sync_playwright

from scripts.flightsfrom_scrape import UA, fetch_rendered_html, parse_rows, WEEKDAY_SHORT

EXPECTED_SCHEDULES: Dict[str, Set[str]] = {
    "WAW": {"FRA", "AMS", "GDN", "KRK", "CPH"},
    "GDN": {"LTN", "STN", "WAW", "CPH"},
    "KRK": {"STN", "AMS", "WAW"},
}
MIN_ROUTE_COUNTS = {"WAW": 80, "GDN": 60, "KRK": 70}
FULL_WEEK = set(WEEKDAY_SHORT)


def fetch_rows_for(origin: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()
        html = fetch_rendered_html(page, origin)
        page.close()
        ctx.close()
        browser.close()
    return parse_rows(html, origin), html


def extract_destination_count(html: str) -> Optional[int]:
    match = re.search(r"You can fly to\s+(\d+)\s+destinations", html)
    if not match:
        return None
    return int(match.group(1))


@pytest.mark.integration
@pytest.mark.parametrize("origin,destinations", EXPECTED_SCHEDULES.items())
def test_direct_routes_operate_every_day(origin: str, destinations: Set[str]):
    """Verify Warsaw/Gdansk/Krakow target flights advertise every weekday."""
    rows, html = fetch_rows_for(origin)
    rows_by_dest = {row.destination_iata: row for row in rows}

    for destination in destinations:
        assert destination in rows_by_dest, f"{origin}->{destination} missing"
        availability = rows_by_dest[destination].operating_days
        weekdays = {day.strip() for day in availability.split(",") if day.strip()}
        assert weekdays == FULL_WEEK, (
            f"{origin}->{destination} should list all weekdays (seen {weekdays})"
        )
    count = extract_destination_count(html)
    assert count is not None, f"destination count missing for {origin}"
    assert count >= MIN_ROUTE_COUNTS[origin], (
        f"{origin} should expose at least {MIN_ROUTE_COUNTS[origin]} destinations (found {count})"
    )
