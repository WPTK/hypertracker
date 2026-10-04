"""SQLite persistence. Plain stdlib sqlite3 — no ORM, low volume, easy to read."""

import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA_V1 = """
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
    created_at   INTEGER NOT NULL,
    manage_token TEXT,                  -- sha256 of the anon edit token (NULL for logged-in owners)
    ends_at      INTEGER                -- epoch of final scheduled arrival (NULL = unknown -> always shown)
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
# Never edit SCHEMA_V1: later changes go in a new numbered migration below.


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    # busy_timeout first so every later pragma (including the journal-mode
    # switch, which takes a lock) waits instead of failing immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers coexist with the (single) writer.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        migrate(conn)


# ---------------- numbered migrations (PRAGMA user_version) ----------------
def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _m1_base_schema(conn):
    """v1: the schema as it stood before versioning, including the old ad-hoc
    ALTERs for databases created by earlier releases."""
    # No executescript(): it commits, which would break the migration's transaction.
    for stmt in SCHEMA_V1.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    if "manage_token" not in _cols(conn, "trips"):
        conn.execute("ALTER TABLE trips ADD COLUMN manage_token TEXT")
    if "ends_at" not in _cols(conn, "trips"):
        conn.execute("ALTER TABLE trips ADD COLUMN ends_at INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_ends ON trips(ends_at)")


def _m2_lifecycle(conn):
    """v2: ends_at NOT NULL (backfilled from leg dates, else created_at + 3 days),
    trips.updated_at, legs.unverified, extra indexes. Rebuilds `trips`; the
    caller turns foreign keys off around it so legs are not cascaded away."""
    from . import lifecycle  # deferred: lifecycle imports db at module level

    if "unverified" not in _cols(conn, "legs"):
        conn.execute("ALTER TABLE legs ADD COLUMN unverified INTEGER DEFAULT 0")

    conn.execute("DELETE FROM legs WHERE trip_id NOT IN (SELECT id FROM trips)")  # orphans

    # Backfill NULL ends_at (and any junk) before the constraint exists.
    for t in conn.execute("SELECT id, created_at FROM trips WHERE ends_at IS NULL").fetchall():
        legs = [dict(l) for l in conn.execute("SELECT * FROM legs WHERE trip_id = ?", (t["id"],)).fetchall()]
        conn.execute(
            "UPDATE trips SET ends_at = ? WHERE id = ?",
            (lifecycle.compute_ends_at(legs, t["created_at"]), t["id"]),
        )

    has_updated = "updated_at" in _cols(conn, "trips")
    conn.execute("DROP TABLE IF EXISTS trips_new")
    conn.execute(
        "CREATE TABLE trips_new ("
        " id           INTEGER PRIMARY KEY AUTOINCREMENT,"
        " owner_id     TEXT NOT NULL,"
        " owner_name   TEXT NOT NULL,"
        " submitter_tz TEXT,"
        " created_at   INTEGER NOT NULL,"
        " manage_token TEXT,"
        " ends_at      INTEGER NOT NULL,"
        " updated_at   INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO trips_new (id, owner_id, owner_name, submitter_tz, created_at, manage_token, ends_at, updated_at) "
        "SELECT id, owner_id, owner_name, submitter_tz, created_at, manage_token, ends_at, "
        + ("COALESCE(updated_at, created_at)" if has_updated else "created_at")
        + " FROM trips"
    )
    conn.execute("DROP TABLE trips")
    conn.execute("ALTER TABLE trips_new RENAME TO trips")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_ends ON trips(ends_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_owner ON trips(owner_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_legs_trip ON legs(trip_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flight_cache_fetched ON flight_cache(fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_cache_fetched ON aircraft_cache(fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_airports_iata ON airports(iata)")


def _m3_identities(conn):
    """v3: anonymous identities. `secret_hash` is sha256 of a secret only the
    browser holds; it outlives any single trip (and its manage token)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS identities ("
        " id          TEXT PRIMARY KEY,"
        " secret_hash TEXT NOT NULL,"
        " created_at  INTEGER NOT NULL,"
        " last_seen   INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_identities_seen ON identities(last_seen)")


MIGRATIONS = [(1, _m1_base_schema), (2, _m2_lifecycle), (3, _m3_identities)]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(conn):
    """Apply pending numbered migrations, each in its own transaction, and
    record progress in PRAGMA user_version. Idempotent; safe on every start."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, fn in MIGRATIONS:
        if number <= version:
            continue
        conn.commit()
        # Foreign keys must be off while a referenced table is rebuilt (the
        # pragma is a no-op inside a transaction, so set it first).
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                bad = conn.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise sqlite3.IntegrityError(f"migration {number}: foreign key violations: {len(bad)}")
                conn.execute(f"PRAGMA user_version = {int(number)}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        version = number


# --- caches ---
_CACHE_TABLES = {"flight_cache": "cache_key", "aircraft_cache": "reg"}


def _check_cache_table(table: str, key_col: str) -> None:
    """Table and column names are interpolated into SQL, so only the known cache
    tables are allowed (values always go through placeholders)."""
    if _CACHE_TABLES.get(table) != key_col:
        raise ValueError(f"unknown cache table {table!r}/{key_col!r}")


def cache_get(table: str, key_col: str, key: str, ttl: int):
    _check_cache_table(table, key_col)
    with get_conn() as conn:
        row = conn.execute(f"SELECT payload, fetched_at FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    if not row:
        return None
    if time.time() - row["fetched_at"] > ttl:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def cache_put(table: str, key_col: str, key: str, payload: dict):
    _check_cache_table(table, key_col)
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
_TYPE_RANK = (
    "CASE type WHEN 'large_airport' THEN 0 WHEN 'medium_airport' THEN 1 "
    "WHEN 'small_airport' THEN 2 ELSE 3 END"
)


def find_airport(code: str):
    """Look up an airport by ICAO ident first, then IATA (deterministic when an
    IATA code is shared: larger airport type first, then ident)."""
    if not code:
        return None
    code = code.strip().upper()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM airports WHERE ident = ?", (code,)).fetchone()
        if not row:
            row = conn.execute(
                f"SELECT * FROM airports WHERE iata = ? ORDER BY {_TYPE_RANK}, ident LIMIT 1", (code,)
            ).fetchone()
    return dict(row) if row else None


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SEARCH_Q_MAX = 40


def search_airports(q: str, limit: int = 8):
    """Prefix matches on code/name/city first, then substring matches in the
    name. Wildcards in the query are escaped; q is clipped to 40 chars."""
    q = (q or "").strip().upper()[:SEARCH_Q_MAX]
    if len(q) < 2:
        return []
    esc = _escape_like(q)
    prefix, contains = esc + "%", "%" + esc + "%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ident, iata, name, municipality, iso_country, lat, lon FROM airports "
            "WHERE ident = :q OR iata = :q OR ident LIKE :p ESCAPE '\\' OR iata LIKE :p ESCAPE '\\' "
            "OR UPPER(name) LIKE :c ESCAPE '\\' OR UPPER(municipality) LIKE :p ESCAPE '\\' "
            "ORDER BY CASE WHEN ident = :q OR iata = :q THEN 0 "
            "              WHEN ident LIKE :p ESCAPE '\\' OR iata LIKE :p ESCAPE '\\' THEN 1 "
            "              WHEN UPPER(name) LIKE :p ESCAPE '\\' OR UPPER(municipality) LIKE :p ESCAPE '\\' THEN 2 "
            "              ELSE 3 END, "
            f"{_TYPE_RANK}, length(name), ident LIMIT :n",
            {"q": q, "p": prefix, "c": contains, "n": limit},
        ).fetchall()
    return [dict(r) for r in rows]
