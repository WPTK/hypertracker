"""Test environment. Set before any app import: config reads env at import time."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["DEV_MODE"] = "false"
os.environ["OPEN_BOARD"] = "true"
os.environ["ADMIN_DISCORD_IDS"] = "111222333"
os.environ["WRITE_RATE_LIMIT"] = "5"
os.environ["WRITE_RATE_WINDOW"] = "600"
os.environ["MAX_LEGS_PER_TRIP"] = "4"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from app import db  # noqa: E402

AIRPORTS = [
    ("KJAX", "JAX", "Jacksonville Intl", 30.49, -81.69),
    ("KDEN", "DEN", "Denver Intl", 39.86, -104.67),
    ("EGLL", "LHR", "Heathrow", 51.47, -0.45),
]


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    db.init_db()
    with db.get_conn() as c:
        for ident, iata, name, lat, lon in AIRPORTS:
            c.execute(
                "INSERT OR REPLACE INTO airports (ident,iata,name,lat,lon,type,iso_country,municipality) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ident, iata, name, lat, lon, "large_airport", "US", name),
            )
    yield
