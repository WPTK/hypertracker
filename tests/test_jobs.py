"""Housekeeping jobs: purge, cache prune, orphan users, refresh, and logging."""

import asyncio
import datetime as dt
import logging

import pytest
from helpers import day

from app import config, db, jobs, lifecycle

T0 = int(dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC).timestamp())
D = 86400


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(lifecycle, "now", lambda: T0)


def add_trip(ends_at, owner="m_a"):
    with db.get_conn() as c:
        return c.execute(
            "INSERT INTO trips (owner_id,owner_name,created_at,ends_at,updated_at) VALUES (?,?,?,?,?)",
            (owner, owner, T0 - 10 * D, ends_at, T0 - 10 * D),
        ).lastrowid


def count(table):
    with db.get_conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_housekeeping_purges_prunes_and_logs(caplog):
    add_trip(T0 - 4 * D)  # past purge horizon (3d)
    keep = add_trip(T0 - 1 * D)  # hidden from board but kept
    with db.get_conn() as c:
        c.execute("INSERT INTO flight_cache VALUES ('old','{}',?)", (T0 - 400 * D,))
        c.execute("INSERT INTO flight_cache VALUES ('new','{}',?)", (T0 - 60,))
        c.execute("INSERT INTO aircraft_cache VALUES ('N1','{}',?)", (T0 - 400 * D,))
        c.execute("INSERT INTO aircraft_cache VALUES ('N2','{}',?)", (T0 - 60,))
        c.execute("INSERT INTO users VALUES ('orphan','o',NULL,?)", (T0 - 40 * D,))
        c.execute("INSERT INTO users VALUES ('recent','r',NULL,?)", (T0 - 1 * D,))
        c.execute("INSERT INTO users VALUES ('m_a','a',NULL,?)", (T0 - 40 * D,))  # owns a trip
    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        counts = jobs.housekeeping_once()
    assert counts == {"trips": 1, "flight_cache": 1, "aircraft_cache": 1, "users": 1, "identities": 0}
    assert count("trips") == 1 and count("flight_cache") == 1 and count("aircraft_cache") == 1
    with db.get_conn() as c:
        assert {r[0] for r in c.execute("SELECT discord_id FROM users")} == {"recent", "m_a"}
        assert c.execute("SELECT id FROM trips").fetchone()[0] == keep
    msgs = [r.getMessage() for r in caplog.records if r.name == "hypertracker.jobs"]
    assert any("purged 1 trips, 1 flight_cache + 1 aircraft_cache rows, 1 orphan users" in m for m in msgs)


def test_housekeeping_deletes_only_stale_tripless_identities(caplog):
    add_trip(T0 + D, owner="m_owns")
    with db.get_conn() as c:
        for ident, seen in (
            ("m_old", T0 - 91 * D),  # stale, no trips: deleted
            ("m_fresh", T0 - 89 * D),  # recent, no trips: kept
            ("m_owns", T0 - 200 * D),  # stale but owns a trip: kept
        ):
            c.execute("INSERT INTO identities VALUES (?,?,?,?)", (ident, "h", T0 - 300 * D, seen))
    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        counts = jobs.housekeeping_once()
    assert counts["identities"] == 1
    with db.get_conn() as c:
        assert {r[0] for r in c.execute("SELECT id FROM identities")} == {"m_fresh", "m_owns"}
    assert any("1 stale identities" in r.getMessage() for r in caplog.records)


def test_housekeeping_logs_exceptions_and_continues(monkeypatch, caplog):
    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(lifecycle, "purge_old_trips", boom)
    with db.get_conn() as c:
        c.execute("INSERT INTO flight_cache VALUES ('old','{}',?)", (T0 - 400 * D,))
    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        counts = jobs.housekeeping_once()
    assert counts["flight_cache"] == 1  # later steps still ran
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and errs[0].exc_info and "disk on fire" in str(errs[0].exc_info[1])


def test_loop_runs_immediately_and_survives_failures(monkeypatch, caplog):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first run fails")
        return {}

    monkeypatch.setattr(jobs, "housekeeping_once", flaky)

    async def go():
        t = asyncio.create_task(jobs.run_housekeeping_loop(interval=0.01))
        await asyncio.sleep(0.2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        asyncio.run(go())
    assert len(calls) >= 2
    assert any("housekeeping loop iteration failed" in r.getMessage() for r in caplog.records)


def test_callsigns_only_inside_live_window():
    t = lambda s: dt.datetime.fromtimestamp(s, dt.UTC).strftime("%Y-%m-%d %H:%MZ")
    tid = add_trip(T0 + 5 * D)
    with db.get_conn() as c:
        for cs, dep, arr in (
            ("INWIN", T0 - 3600, T0 + 3600),
            ("SOON", T0 + 10 * 60, T0 + 7200),
            ("TOOSOON", T0 + 3600, T0 + 7200),
            ("LANDED", T0 - 7200, T0 - 50 * 60),
            ("JUSTLANDED", T0 - 7200, T0 - 30 * 60),
        ):
            c.execute(
                "INSERT INTO legs (trip_id,direction,seq,callsign,dep_utc,arr_utc) VALUES (?,?,?,?,?,?)",
                (tid, "out", 0, cs, t(dep), t(arr)),
            )
    assert jobs.compute_callsigns() == {"INWIN", "SOON", "JUSTLANDED"}
    asyncio.run(jobs.refresh_callsigns_once())
    assert jobs.callsign_supplier() == frozenset({"INWIN", "SOON", "JUSTLANDED"})


# ---------------- refresh_upcoming ----------------
def add_flight_leg(tid, flight_no, dep_ts, reg=None):
    f = lambda s: dt.datetime.fromtimestamp(s, dt.UTC).strftime("%Y-%m-%d %H:%MZ")
    with db.get_conn() as c:
        return c.execute(
            "INSERT INTO legs (trip_id,direction,seq,date_local,flight_no,dep_utc,arr_utc,reg,manual) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (tid, "out", 0, day(0), flight_no, f(dep_ts), f(dep_ts + 7200), reg),
        ).lastrowid


def install_refresh(monkeypatch, fn):
    import app.resolver as resolver

    monkeypatch.setattr(resolver, "refresh_leg", fn, raising=False)


def test_refresh_upcoming_updates_legs_lacking_reg(monkeypatch):
    jobs.reset_refresh_state()
    tid = add_trip(T0 + 2 * D)
    due = add_flight_leg(tid, "DL1", T0 + 10 * 3600)
    far = add_flight_leg(tid, "DL2", T0 + 5 * D)  # beyond 48h
    has_reg = add_flight_leg(tid, "DL3", T0 + 10 * 3600, reg="N1")
    seen = []

    async def refresh(leg):
        seen.append(leg["id"])
        return {"reg": "N999", "ac_type": "B738", "id": 123456, "bogus": 1}

    install_refresh(monkeypatch, refresh)
    assert asyncio.run(jobs.refresh_upcoming()) == 1
    assert seen == [due] and far not in seen and has_reg not in seen
    with db.get_conn() as c:
        row = c.execute("SELECT reg, ac_type, id FROM legs WHERE id = ?", (due,)).fetchone()
    assert (row["reg"], row["ac_type"], row["id"]) == (
        "N999",
        "B738",
        due,
    )  # only whitelisted columns applied


def test_refresh_upcoming_respects_budget_and_spacing(monkeypatch):
    jobs.reset_refresh_state()
    monkeypatch.setattr(config, "MAX_REFRESH_PER_DAY", 2, raising=False)
    tid = add_trip(T0 + 2 * D)
    for i in range(4):
        add_flight_leg(tid, f"DL{i}", T0 + 3600 * (i + 1))
    n = []

    async def refresh(leg):
        n.append(leg["id"])
        return None

    install_refresh(monkeypatch, refresh)
    asyncio.run(jobs.refresh_upcoming())
    assert len(n) == 2
    asyncio.run(jobs.refresh_upcoming())
    assert len(n) == 2  # budget exhausted for the day
    monkeypatch.setattr(lifecycle, "now", lambda: T0 + D)  # next UTC day: fresh budget
    add_flight_leg(tid, "DL9", T0 + D + 3600)
    asyncio.run(jobs.refresh_upcoming())
    assert len(n) > 2


def test_refresh_upcoming_skips_when_refresh_leg_missing(monkeypatch, caplog):
    import app.resolver as resolver

    monkeypatch.delattr(resolver, "refresh_leg", raising=False)
    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        assert asyncio.run(jobs.refresh_upcoming()) == 0
    assert any("refresh_leg" in r.getMessage() for r in caplog.records)


def test_refresh_failure_is_logged_not_raised(monkeypatch, caplog):
    jobs.reset_refresh_state()
    tid = add_trip(T0 + 2 * D)
    add_flight_leg(tid, "DL1", T0 + 3600)

    async def refresh(leg):
        raise RuntimeError("upstream exploded")

    install_refresh(monkeypatch, refresh)
    with caplog.at_level(logging.INFO, logger="hypertracker.jobs"):
        assert asyncio.run(jobs.refresh_upcoming()) == 0
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)
