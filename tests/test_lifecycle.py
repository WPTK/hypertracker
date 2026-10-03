"""Lifecycle rules with an injectable clock (`app.lifecycle.now`)."""

import datetime as dt

import pytest
from helpers import manual_row

from app import config, db, lifecycle

pytestmark = pytest.mark.usefixtures("fake_resolver")

T0 = int(dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC).timestamp())
H = 3600
D = 86400


def at(monkeypatch, t):
    monkeypatch.setattr(lifecycle, "now", lambda: t)


def post(client, legs, name="Alex"):
    r = client.post("/api/trips", json={"name": name, "out": legs})
    assert r.status_code == 200, r.text
    return r.json()["trip_id"]


def visible(client):
    return {t["id"] for t in client.get("/api/trips").json()["trips"]}


def exists(tid):
    with db.get_conn() as c:
        return c.execute("SELECT 1 FROM trips WHERE id = ?", (tid,)).fetchone() is not None


def ends_at(tid):
    with db.get_conn() as c:
        return c.execute("SELECT ends_at FROM trips WHERE id = ?", (tid,)).fetchone()[0]


# ---- compute_ends_at ----
def test_compute_ends_at_prefers_arrival():
    legs = [{"arr_utc": "2026-10-03 16:00Z", "date_local": "2026-10-03"}]
    assert lifecycle.compute_ends_at(legs) == int(dt.datetime(2026, 10, 3, 16, tzinfo=dt.UTC).timestamp())


def test_compute_ends_at_date_only_is_end_of_day_plus_36h():
    got = lifecycle.compute_ends_at([{"date_local": "2026-10-03"}])
    assert got == int(dt.datetime(2026, 10, 4, tzinfo=dt.UTC).timestamp()) + 36 * H


def test_compute_ends_at_is_max_over_all_legs():
    legs = [{"arr_utc": "2026-09-01 10:00Z"}, {"arr_utc": None, "date_local": "2026-09-20"}]
    got = lifecycle.compute_ends_at(legs, created_at=T0)
    assert got == int(dt.datetime(2026, 9, 21, tzinfo=dt.UTC).timestamp()) + 36 * H
    assert got > lifecycle.compute_ends_at([legs[0]], created_at=T0)


def test_compute_ends_at_never_none():
    assert lifecycle.compute_ends_at([], created_at=T0) == T0 + 3 * D
    assert (
        lifecycle.compute_ends_at([{"date_local": "garbage", "arr_utc": "nope"}], created_at=T0) == T0 + 3 * D
    )
    assert isinstance(lifecycle.compute_ends_at([{}]), int)


def test_overnight_and_timezone_edge():
    # Arrival after local midnight still ends by the date-based rule (+36h covers any zone).
    day_end = int(dt.datetime(2026, 10, 4, tzinfo=dt.UTC).timestamp())
    assert lifecycle.compute_ends_at([{"date_local": "2026-10-03"}]) >= day_end + 24 * H


def test_parse_adb_dt_formats():
    assert lifecycle.parse_adb_dt("2026-10-10 12:15Z").hour == 12
    assert lifecycle.parse_adb_dt("2026-10-10 08:15-04:00").hour == 12
    assert lifecycle.parse_adb_dt("junk") is None and lifecycle.parse_adb_dt(None) is None


# ---- repros from the plan ----
def test_manual_only_trip_expires_and_is_purged(client, monkeypatch):
    at(monkeypatch, T0)
    tid = post(client, [manual_row(date="2026-10-03")])
    end = ends_at(tid)
    assert end == int(dt.datetime(2026, 10, 4, tzinfo=dt.UTC).timestamp()) + 36 * H  # never NULL

    grace = config.TRIP_GRACE_HOURS * H
    at(monkeypatch, end + grace - 1)
    assert tid in visible(client)  # grace boundary: still shown
    at(monkeypatch, end + grace + 1)
    assert tid not in visible(client)  # hidden from the board...
    assert exists(tid)  # ...but not deleted yet
    assert lifecycle.purge_old_trips() == 0

    at(monkeypatch, end + config.TRIP_PURGE_DAYS * D - 1)
    assert lifecycle.purge_old_trips() == 0  # purge boundary
    at(monkeypatch, end + config.TRIP_PURGE_DAYS * D + 1)
    assert lifecycle.purge_old_trips() == 1
    assert not exists(tid)
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM legs WHERE trip_id = ?", (tid,)).fetchone()[0] == 0


def test_mixed_trip_stays_until_last_leg_lands(client, fake_resolver, monkeypatch):
    fake_resolver.flights["DL1200"] = ("KJAX", "KDEN", "2026-10-03 12:00Z", "2026-10-03 16:00Z", "N1")
    at(monkeypatch, T0)
    r = client.post(
        "/api/trips",
        json={
            "name": "Mix",
            "out": [{"flight_no": "DL1200", "date": "2026-10-03"}],
            "ret": [manual_row(date="2026-10-10", frm="DEN", to="JAX")],
        },
    )
    assert r.status_code == 200, r.text
    tid = r.json()["trip_id"]
    # Two days after the outbound landed the trip is still on the board (old bug: gone at +8h).
    at(monkeypatch, int(dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC).timestamp()))
    assert tid in visible(client)
    ret_end = lifecycle.compute_ends_at([{"date_local": "2026-10-10"}])
    assert ends_at(tid) == ret_end
    at(monkeypatch, ret_end + config.TRIP_GRACE_HOURS * H + 1)
    assert tid not in visible(client)


def test_purge_disabled_when_zero_days(client, monkeypatch):
    at(monkeypatch, T0)
    tid = post(client, [manual_row(date="2026-10-03")])
    monkeypatch.setattr(config, "TRIP_PURGE_DAYS", 0)
    at(monkeypatch, T0 + 400 * D)
    assert lifecycle.purge_old_trips() == 0 and exists(tid)


def test_edit_recomputes_ends_at_and_bumps_updated_at(client, monkeypatch):
    at(monkeypatch, T0)
    r = client.post("/api/trips", json={"name": "E", "out": [manual_row(date="2026-10-03")]}).json()
    before = ends_at(r["trip_id"])
    at(monkeypatch, T0 + 10)
    client.put(
        f"/api/trips/{r['trip_id']}",
        headers={"X-Manage-Token": r["manage_token"]},
        json={"out": [manual_row(date="2026-10-20")]},
    )
    assert ends_at(r["trip_id"]) > before
    with db.get_conn() as c:
        assert (
            c.execute("SELECT updated_at FROM trips WHERE id = ?", (r["trip_id"],)).fetchone()[0] >= T0 + 10
        )
