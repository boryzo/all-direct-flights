import csv
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Set

import pytest

EXPECTED_SCHEDULES: Dict[str, Set[str]] = {
    "WAW": {"FRA", "AMS", "GDN", "KRK", "CPH"},
    "GDN": {"LTN", "STN", "WAW", "CPH"},
    "KRK": {"STN", "AMS", "WAW"},
}
MIN_ROUTE_COUNTS = {"WAW": 80, "GDN": 60, "KRK": 70}
FULL_WEEK = "1-7"


def run_scraper(origin: str, out_dir: Path) -> Path:
    script = Path(__file__).resolve().parents[2] / "scripts" / "flightsfrom_scrape.py"
    cmd = [
        sys.executable,
        str(script),
        f"--airports={origin}",
        "--out-dir",
        str(out_dir),
    ]
    subprocess.run(cmd, check=True)
    out_path = out_dir / f"{origin}.csv"
    assert out_path.exists(), f"missing csv for {origin}"
    return out_path


def load_rows(csv_path: Path):
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.integration
@pytest.mark.parametrize("origin,destinations", EXPECTED_SCHEDULES.items())
def test_direct_routes_operate_every_day(origin: str, destinations: Set[str]):
    """Verify Warsaw/Gdansk/Krakow target flights advertise every weekday."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir)
        csv_path = run_scraper(origin, out_dir)
        rows = load_rows(csv_path)

    rows_by_dest = {row["destination_iata"]: row for row in rows}

    for destination in destinations:
        assert destination in rows_by_dest, f"{origin}->{destination} missing"
        availability = rows_by_dest[destination]["operating_days"]
        assert availability == FULL_WEEK, (
            f"{origin}->{destination} should list all weekdays (seen {availability})"
        )
    assert len(rows) >= MIN_ROUTE_COUNTS[origin], (
        f"{origin} should expose at least {MIN_ROUTE_COUNTS[origin]} destinations (found {len(rows)})"
    )
