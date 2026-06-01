"""SQLite persistence. Plain stdlib sqlite3 — no ORM, low volume, easy to read."""
import sqlite3
import json
import time
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    tz         TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS trips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     TEXT NOT NULL,
    owner_name   TEXT NOT NULL,
    submitter_tz TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS legs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    direction   TEXT NOT NULL,          -- 'out' or 'ret'
    seq         INTEGER NOT NULL,       -- order within a direction (0,1,2 for connections)
    date_local  TEXT,                   -- departure-airport-local date (YYYY-MM-DD)
    flight_no   TEXT,                   -- as entered, normalised (e.g. DL1200)
    callsign    TEXT,                   -- ICAO callsign (e.g. DAL1200)
    dep_icao TEXT, dep_iata TEXT, dep_name TEXT, dep_lat REAL, dep_lon REAL,
    dep_local TEXT, dep_utc TEXT,
    arr_icao TEXT, arr_iata TEXT, arr_name TEXT, arr_lat REAL, arr_lon REAL,
    arr_local TEXT, arr_utc TEXT,
    reg         TEXT,
    ac_type     TEXT,                   -- ICAO type code (e.g. B739) if known
    ac_model    TEXT,                   -- human model (e.g. Boeing 737-900)
    ac_age      REAL,                   -- years, 1 dp
    ac_built    TEXT,                   -- best-known build/first-flight date
    resolved    INTEGER DEFAULT 0,      -- 1 if AeroDataBox resolved it
    manual      INTEGER DEFAULT 0       -- 1 if airports were entered by hand
);

CREATE TABLE IF NOT EXISTS flight_cache (
    cache_key  TEXT PRIMARY KEY,        -- "<flight_no>|<date_local>"
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS aircraft_cache (
    reg        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS airports (
    ident        TEXT PRIMARY KEY,      -- ICAO / OurAirports ident
    iata         TEXT,
    name         TEXT,
    lat          REAL,
    lon          REAL,
    type         TEXT,
    iso_country  TEXT,
    municipality TEXT
);
CREATE INDEX IF NOT EXISTS idx_airports_iata ON airports(iata);
CREATE INDEX IF NOT EXISTS idx_legs_trip ON legs(trip_id);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# --- caches ---
def cache_get(table: str, key_col: str, key: str, ttl: int):
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT payload, fetched_at FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()
    if not row:
        return None
    if time.time() - row["fetched_at"] > ttl:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def cache_put(table: str, key_col: str, key: str, payload: dict):
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {table} ({key_col}, payload, fetched_at) VALUES (?,?,?) "
            f"ON CONFLICT({key_col}) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (key, json.dumps(payload), int(time.time())),
        )


# --- users ---
def upsert_user(discord_id: str, username: str, tz: str | None = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (discord_id, username, tz, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET username=excluded.username, "
            "tz=COALESCE(excluded.tz, users.tz), updated_at=excluded.updated_at",
            (discord_id, username, tz, int(time.time())),
        )


# --- airports ---
def find_airport(code: str):
    """Look up an airport by ICAO ident first, then IATA."""
    if not code:
        return None
    code = code.strip().upper()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM airports WHERE ident = ?", (code,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM airports WHERE iata = ? LIMIT 1", (code,)
            ).fetchone()
    return dict(row) if row else None


def search_airports(q: str, limit: int = 8):
    q = (q or "").strip().upper()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ident, iata, name, municipality, iso_country, lat, lon FROM airports "
            "WHERE ident = ? OR iata = ? OR ident LIKE ? OR iata LIKE ? OR UPPER(name) LIKE ? "
            "ORDER BY CASE WHEN ident=? OR iata=? THEN 0 ELSE 1 END, length(name) LIMIT ?",
            (q, q, like, like, like, q, q, limit),
        ).fetchall()
    return [dict(r) for r in rows]
