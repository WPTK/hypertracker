"""Test environment. Set before any app import: config reads env at import time.

Every test gets its own database file and fresh rate-limit state, an offline
fake resolver, and the real clock unless it patches `app.lifecycle.now`."""

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
os.environ["TRUSTED_PROXY"] = "none"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from helpers import seed_airports  # noqa: E402

from app import config, db, jobs, netutil  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Per-test DB and rate-limit/job state."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    # Tests share one client address; rate-limit tests set their own limits.
    monkeypatch.setattr(config, "WRITE_RATE_LIMIT", 1000)
    db.init_db()
    seed_airports()
    netutil.reset_rate_limits()
    jobs.reset_refresh_state()
    yield


class FakeResolver:
    """Stand-in for app.resolver.resolve_rows. Legs are built from the local
    airports table. `flights` maps flight_no -> (from, to, dep_utc, arr_utc,
    reg); `override` maps flight_no -> status to force a failure."""

    def __init__(self):
        self.flights = {
            "DL1200": ("KJAX", "KDEN", "2099-01-01 12:00Z", "2099-01-01 16:00Z", "N123DL"),
            "BA217": ("EGLL", "KDEN", "2099-01-02 10:00Z", "2099-01-02 18:00Z", None),
        }
        self.override: dict[str, str] = {}
        self.calls: list[list[dict]] = []

    def leg_for(self, row, a, b, dep, arr, reg):
        def ap(code):
            r = db.find_airport(code)
            return r

        da, aa = ap(a), ap(b)
        return {
            "direction": row["direction"],
            "seq": row["seq"],
            "date_local": row["date"],
            "flight_no": row["flight_no"],
            "callsign": "TST" + row["flight_no"][2:],
            "dep_icao": da["ident"],
            "dep_iata": da["iata"],
            "dep_name": da["name"],
            "dep_lat": da["lat"],
            "dep_lon": da["lon"],
            "dep_local": dep,
            "dep_utc": dep,
            "arr_icao": aa["ident"],
            "arr_iata": aa["iata"],
            "arr_name": aa["name"],
            "arr_lat": aa["lat"],
            "arr_lon": aa["lon"],
            "arr_local": arr,
            "arr_utc": arr,
            "reg": reg,
            "ac_type": "B738" if reg else None,
            "ac_model": None,
            "ac_age": None,
            "ac_built": None,
            "resolved": 1,
            "manual": 0,
            "status": "ok",
            "message": "",
            "candidates": [],
            "unverified": False,
        }

    async def __call__(self, rows, *, deadline=20.0):
        self.calls.append(rows)
        out = []
        for row in rows:
            fn = row["flight_no"]
            status = self.override.get(fn)
            if status:
                leg = {
                    "direction": row["direction"],
                    "seq": row["seq"],
                    "date_local": row["date"],
                    "flight_no": fn,
                    "resolved": 0,
                    "manual": 0,
                    "status": status,
                    "message": f"fake {status}",
                    "candidates": [],
                    "unverified": False,
                }
            elif fn in self.flights:
                leg = self.leg_for(row, *self.flights[fn])
            else:
                leg = {
                    "direction": row["direction"],
                    "seq": row["seq"],
                    "date_local": row["date"],
                    "flight_no": fn,
                    "resolved": 0,
                    "manual": 0,
                    "status": "not_found",
                    "message": "I couldn't find that flight on that date.",
                    "candidates": [],
                    "unverified": False,
                }
            out.append(leg)
        return out


@pytest.fixture
def fake_resolver(monkeypatch):
    import app.resolver as resolver_mod

    fake = FakeResolver()
    monkeypatch.setattr(resolver_mod, "resolve_rows", fake, raising=False)
    return fake


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)
