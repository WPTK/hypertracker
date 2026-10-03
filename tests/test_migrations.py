"""Numbered migrations: a v0 database with NULL ends_at rows is upgraded in place."""

import sqlite3

import pytest

from app import config, db
from app.lifecycle import compute_ends_at

# The schema as shipped before migrations existed (ends_at nullable, no updated_at).
V0_SCHEMA = db.SCHEMA_V1

OLDEST_TRIPS = """
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL,
    submitter_tz TEXT, created_at INTEGER NOT NULL
);
"""

CREATED = 1_700_000_000


def make_v0(path, with_old_columns=True):
    conn = sqlite3.connect(path)
    if with_old_columns:
        conn.executescript(V0_SCHEMA)
    else:
        conn.executescript(OLDEST_TRIPS)
        conn.executescript(
            "CREATE TABLE legs (id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL "
            "REFERENCES trips(id) ON DELETE CASCADE, direction TEXT NOT NULL, seq INTEGER NOT NULL, "
            "date_local TEXT, flight_no TEXT, callsign TEXT, dep_icao TEXT, dep_iata TEXT, dep_name TEXT, "
            "dep_lat REAL, dep_lon REAL, dep_local TEXT, dep_utc TEXT, arr_icao TEXT, arr_iata TEXT, "
            "arr_name TEXT, arr_lat REAL, arr_lon REAL, arr_local TEXT, arr_utc TEXT, reg TEXT, ac_type TEXT, "
            "ac_model TEXT, ac_age REAL, ac_built TEXT, resolved INTEGER DEFAULT 0, manual INTEGER DEFAULT 0);"
            "CREATE TABLE flight_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL);"
            "CREATE TABLE aircraft_cache (reg TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL);"
            "CREATE TABLE users (discord_id TEXT PRIMARY KEY, username TEXT NOT NULL, tz TEXT, updated_at INTEGER);"
            "CREATE TABLE airports (ident TEXT PRIMARY KEY, iata TEXT, name TEXT, lat REAL, lon REAL, type TEXT, "
            "iso_country TEXT, municipality TEXT);"
        )
    cols = (
        "(owner_id, owner_name, created_at, ends_at)"
        if with_old_columns
        else "(owner_id, owner_name, created_at)"
    )
    ph = "(?,?,?,?)" if with_old_columns else "(?,?,?)"

    def trip(owner, ends=None):
        args = (owner, owner, CREATED) + ((ends,) if with_old_columns else ())
        return conn.execute(f"INSERT INTO trips {cols} VALUES {ph}", args).lastrowid

    def leg(tid, seq, **kw):
        conn.execute(
            "INSERT INTO legs (trip_id, direction, seq, date_local, arr_utc, flight_no, manual) "
            "VALUES (?, 'out', ?, ?, ?, ?, ?)",
            (tid, seq, kw.get("date"), kw.get("arr"), kw.get("fn"), kw.get("manual", 0)),
        )

    t_manual = trip("manual")  # NULL ends_at, manual leg with a date
    leg(t_manual, 0, date="2025-01-01", manual=1)
    t_mixed = trip("mixed")  # NULL ends_at, resolved leg + later manual leg
    leg(t_mixed, 0, date="2025-03-01", arr="2025-03-01 16:00Z", fn="DL1")
    leg(t_mixed, 1, date="2025-03-10", manual=1)
    t_nodate = trip("nodate")  # NULL ends_at, no usable date at all
    leg(t_nodate, 0)
    t_empty = trip("empty")  # NULL ends_at, no legs
    t_ok = trip("ok", ends=1_800_000_000 if with_old_columns else None)
    leg(t_ok, 0, date="2027-01-01", arr="2027-01-15 10:00Z")
    conn.execute("INSERT INTO flight_cache VALUES ('k','{}',1)")
    conn.commit()
    conn.close()
    return dict(manual=t_manual, mixed=t_mixed, nodate=t_nodate, empty=t_empty, ok=t_ok)


def ts(s):
    import datetime as dt

    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())


@pytest.fixture
def dbfile(tmp_path, monkeypatch):
    p = str(tmp_path / "old.db")
    monkeypatch.setattr(config, "DB_PATH", p)
    return p


def rows(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("old_columns", [True, False])
def test_v0_with_null_ends_at_is_backfilled(dbfile, old_columns):
    ids = make_v0(dbfile, with_old_columns=old_columns)
    assert rows(dbfile, "PRAGMA user_version")[0][0] == 0
    db.init_db()
    assert rows(dbfile, "PRAGMA user_version")[0][0] == db.SCHEMA_VERSION == 3

    ends = {r["id"]: r["ends_at"] for r in rows(dbfile, "SELECT id, ends_at FROM trips")}
    assert all(v is not None for v in ends.values())
    day_slack = 36 * 3600 + 86400
    assert ends[ids["manual"]] == ts("2025-01-01T00:00:00") + day_slack
    assert ends[ids["mixed"]] == ts("2025-03-10T00:00:00") + day_slack  # max over ALL legs
    assert ends[ids["nodate"]] == CREATED + 3 * 86400
    assert ends[ids["empty"]] == CREATED + 3 * 86400
    if old_columns:
        assert ends[ids["ok"]] == 1_800_000_000  # existing value untouched
    else:
        assert ends[ids["ok"]] == ts("2027-01-15T10:00:00")


def test_migration_keeps_data_and_adds_columns(dbfile):
    ids = make_v0(dbfile)
    db.init_db()
    assert len(rows(dbfile, "SELECT * FROM legs")) == 5  # legs survived the trips rebuild
    assert rows(dbfile, "SELECT COUNT(*) FROM flight_cache")[0][0] == 1
    cols = {r["name"]: r for r in rows(dbfile, "PRAGMA table_info(trips)")}
    assert cols["ends_at"]["notnull"] == 1 and "updated_at" in cols
    assert {r["updated_at"] for r in rows(dbfile, "SELECT updated_at FROM trips")} == {CREATED}
    assert "unverified" in {r["name"] for r in rows(dbfile, "PRAGMA table_info(legs)")}
    idx = {r["name"] for r in rows(dbfile, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_trips_ends", "idx_trips_owner", "idx_legs_trip"} <= idx
    assert not rows(dbfile, "PRAGMA foreign_key_check")
    assert ids["ok"] in {r["id"] for r in rows(dbfile, "SELECT id FROM trips")}


def test_ends_at_not_null_is_enforced_and_cascade_still_works(dbfile):
    ids = make_v0(dbfile)
    db.init_db()
    with db.get_conn() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO trips (owner_id, owner_name, created_at, ends_at) VALUES ('a','b',1,NULL)")
    with db.get_conn() as c:
        c.execute("DELETE FROM trips WHERE id = ?", (ids["mixed"],))
    assert rows(dbfile, f"SELECT COUNT(*) FROM legs WHERE trip_id = {ids['mixed']}")[0][0] == 0


def test_migration_is_idempotent(dbfile):
    make_v0(dbfile)
    db.init_db()
    before = [tuple(r) for r in rows(dbfile, "SELECT * FROM trips ORDER BY id")]
    db.init_db()
    db.init_db()
    assert [tuple(r) for r in rows(dbfile, "SELECT * FROM trips ORDER BY id")] == before
    assert rows(dbfile, "PRAGMA user_version")[0][0] == 3


def test_v2_database_gains_identities_table(dbfile, monkeypatch):
    make_v0(dbfile)
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])  # build a genuine v2 database
        db.init_db()
    assert rows(dbfile, "PRAGMA user_version")[0][0] == 2
    assert not rows(dbfile, "SELECT name FROM sqlite_master WHERE name = 'identities'")
    before = [tuple(r) for r in rows(dbfile, "SELECT * FROM trips ORDER BY id")]
    db.init_db()
    db.init_db()  # idempotent
    assert rows(dbfile, "PRAGMA user_version")[0][0] == 3
    cols = {r["name"]: r for r in rows(dbfile, "PRAGMA table_info(identities)")}
    assert set(cols) == {"id", "secret_hash", "created_at", "last_seen"}
    assert cols["id"]["pk"] == 1 and all(
        cols[c]["notnull"] for c in ("secret_hash", "created_at", "last_seen")
    )
    assert [tuple(r) for r in rows(dbfile, "SELECT * FROM trips ORDER BY id")] == before


def test_fresh_database_reaches_latest_version(dbfile):
    db.init_db()
    assert rows(dbfile, "PRAGMA user_version")[0][0] == db.SCHEMA_VERSION
    names = {r["name"] for r in rows(dbfile, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "trips", "legs", "flight_cache", "aircraft_cache", "airports", "identities"} <= names


def test_failed_migration_rolls_back(dbfile, monkeypatch):
    make_v0(dbfile)

    def explode(conn):
        conn.execute("ALTER TABLE legs ADD COLUMN junk TEXT")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", [(1, db._m1_base_schema), (2, explode)])
    with pytest.raises(RuntimeError):
        db.init_db()
    assert rows(dbfile, "PRAGMA user_version")[0][0] == 1
    assert "junk" not in {r["name"] for r in rows(dbfile, "PRAGMA table_info(legs)")}


def test_busy_timeout_set_before_journal_mode(monkeypatch, dbfile):
    seen = []
    real = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            seen.append(sql)
            return self._c.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._c, n)

        @property
        def row_factory(self):
            return self._c.row_factory

        @row_factory.setter
        def row_factory(self, v):
            self._c.row_factory = v

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Spy(real(*a, **k)))
    with db.get_conn():
        pass
    pragmas = [s for s in seen if s.startswith("PRAGMA")]
    assert pragmas.index("PRAGMA busy_timeout = 5000") < pragmas.index("PRAGMA journal_mode = WAL")


def test_compute_ends_at_used_by_backfill_matches_runtime_rule():
    assert (
        compute_ends_at([{"date_local": "2025-01-01"}], CREATED)
        == ts("2025-01-01T00:00:00") + 36 * 3600 + 86400
    )
