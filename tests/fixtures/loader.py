"""Shared helpers for the resolution tests: fixture loading, a recording mock
upstream, and an isolated temp database. Import with `from fixtures.loader import ...`.
"""

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

FIX = Path(__file__).parent


def today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def future_date(days: int = 10) -> str:
    return (today() + dt.timedelta(days=days)).isoformat()


def load(name: str, date: str | None = None):
    """Load a fixture. Synthetic fixtures use 2026-10-10 / 2026-10-11 as
    placeholders; pass `date` to rewrite them (10-11 becomes date + 1 day)."""
    text = (FIX / name).read_text()
    if date:
        nxt = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        text = text.replace("2026-10-11", nxt).replace("2026-10-10", date)
    return json.loads(text)


class Upstream:
    """httpx.MockTransport handler that records requests and plays responses.

    `script` is a list of responses consumed in order (the last one repeats):
    an httpx.Response, a (status, json_body) tuple, or an Exception to raise.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        status, body = item
        if body is None:
            return httpx.Response(status)
        if isinstance(body, str) and not body.startswith(("{", "[", '"')):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    @property
    def transport(self):
        return httpx.MockTransport(self)

    @property
    def calls(self) -> int:
        return len(self.requests)


def resp(status: int, name: str, date: str | None = None) -> httpx.Response:
    """A JSON response whose body is a fixture file (rewritten for `date`)."""
    text = (FIX / name).read_text()
    if date:
        nxt = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        text = text.replace("2026-10-11", nxt).replace("2026-10-10", date)
    return httpx.Response(status, content=text.encode(), headers={"content-type": "application/json"})


AIRPORTS = [
    ("KJAX", "JAX", "Jacksonville Intl", 30.49, -81.69, "Jacksonville"),
    ("KDEN", "DEN", "Denver Intl", 39.86, -104.67, "Denver"),
    ("KATL", "ATL", "Atlanta Hartsfield-Jackson", 33.64, -84.43, "Atlanta"),
    ("EGLL", "LHR", "Heathrow", 51.47, -0.45, "London"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp DB with a few airports, a fake API key, no real sleeping, and a
    clean breaker. Yields a helper with `.use(upstream)` to install a mock."""
    from app import aerodatabox as adb
    from app import airplaneslive, config, db

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "AERODATABOX_KEY", "secret-test-key")
    monkeypatch.setattr(config, "AERODATABOX_BASE", "https://adb.test")
    monkeypatch.setattr(config, "AERODATABOX_AUTH", "rapidapi", raising=False)
    db.init_db()
    with db.get_conn() as c:
        for ident, iata, name, lat, lon, city in AIRPORTS:
            c.execute(
                "INSERT OR REPLACE INTO airports (ident,iata,name,lat,lon,type,iso_country,municipality) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ident, iata, name, lat, lon, "large_airport", "US", city),
            )
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(adb, "_sleep", fake_sleep)
    monkeypatch.setattr(adb, "_MIN_INTERVAL", 0.0)  # pacing has its own tests
    adb.reset_state()
    airplaneslive.reset()

    class Env:
        sleeps = slept

        def use(self, upstream):
            adb.set_transport(upstream.transport)
            return upstream

    yield Env()
    adb.set_transport(None)
    adb.reset_state()
    airplaneslive.reset()
