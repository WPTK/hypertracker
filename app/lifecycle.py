"""Lifecycle (when a trip drops off the board), shaping trips for the API, and
purging long-dead rows.

Rules, written once:
  * A leg ends at its scheduled arrival (arr_utc) when known, otherwise at the
    end of its local date (UTC) plus 36 hours. A leg with no usable date ends
    3 days after the trip was created.
  * A trip ends at the latest end over ALL its legs, and never has no end.
  * The board hides a trip at ends_at + TRIP_GRACE_HOURS; the purge job
    deletes it at ends_at + TRIP_PURGE_DAYS (0 keeps it forever).

This module must not import the resolver at import time (the resolver imports
db, and db's migrations import this module lazily).
"""

import datetime as dt
import time
from urllib.parse import quote

from . import airplaneslive, config, db, flightstatus

_DAY = 86400
LEG_DATE_SLACK_HOURS = 36
NO_DATE_FALLBACK_DAYS = 3


def now() -> int:
    """Current epoch seconds. Patch this in tests for time-dependent logic."""
    return int(time.time())


def parse_adb_dt(value: str | None):
    """Parse an AeroDataBox time string like '2026-06-04 12:00Z' or
    '2026-06-04 08:00-04:00' to an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
        return d.astimezone(dt.UTC)
    except (ValueError, TypeError):
        return None


def _parse_date(value: str | None):
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def leg_ends_at(leg: dict, created_at: int) -> int:
    """When one leg is over (epoch seconds). Never None."""
    arr = parse_adb_dt(leg.get("arr_utc"))
    if arr:
        return int(arr.timestamp())
    d = _parse_date(leg.get("date_local"))
    if d:
        end_of_day = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
        return int(end_of_day.timestamp()) + LEG_DATE_SLACK_HOURS * 3600
    return int(created_at) + NO_DATE_FALLBACK_DAYS * _DAY


def compute_ends_at(legs: list[dict], created_at: int | None = None) -> int:
    """The trip's end: the max over ALL legs. Never returns None."""
    created = int(created_at) if created_at else now()
    ends = [leg_ends_at(l, created) for l in legs]
    return max(ends) if ends else created + NO_DATE_FALLBACK_DAYS * _DAY


def _active_cutoff() -> int:
    """A trip is on the board until TRIP_GRACE_HOURS after its end."""
    return now() - config.TRIP_GRACE_HOURS * 3600


def fa_url(callsign: str | None, flight_no: str | None) -> str | None:
    ident = (callsign or flight_no or "").strip()
    if not ident:
        return None
    return f"https://flightaware.com/live/flight/{quote(ident, safe='')}"


def _live_state(leg: dict, now_dt: dt.datetime) -> str | None:
    """In-memory read from the poller, only inside the leg's live window."""
    callsign = leg.get("callsign")
    getter = getattr(airplaneslive, "get_state", None)
    if not callsign or getter is None:
        return None
    dep = parse_adb_dt(leg.get("dep_utc"))
    arr = parse_adb_dt(leg.get("arr_utc"))
    if not (dep and arr):
        return None
    if not (dep - dt.timedelta(minutes=20) <= now_dt <= arr + dt.timedelta(minutes=45)):
        return None
    try:
        state = getter(callsign)
    except Exception:
        return None
    return state if state in ("airborne", "on_ground") else None


def _effective_live_state(leg: dict, fs: dict, now_dt: dt.datetime) -> str | None:
    """The state the board and the map colour by. airplanes.live wins when it sees the
    plane; otherwise AeroDataBox's "en route" counts, because polar and oceanic legs have
    almost no ADS-B coverage and would sit on "scheduled" for the whole flight."""
    state = _live_state(leg, now_dt)
    if state is not None or fs.get("state") != flightstatus.AIRBORNE:
        return state
    dep = parse_adb_dt(fs.get("dep_utc_est") or leg.get("dep_utc"))
    arr = parse_adb_dt(fs.get("arr_utc_est") or leg.get("arr_utc"))
    if dep and arr and dep - dt.timedelta(minutes=20) <= now_dt <= arr + dt.timedelta(minutes=45):
        return "airborne"
    return None


def shape_trip(trip_row: dict, leg_rows: list, cities: dict | None = None) -> dict:
    """Public shape of one trip. Pure CPU / in-memory: no network, no DB."""
    cities = cities or {}
    now_dt = dt.datetime.fromtimestamp(now(), dt.UTC)
    out, ret = [], []
    for r in leg_rows:
        l = dict(r)
        fs = flightstatus.get(l) or {}
        item = {
            "direction": l["direction"],
            "seq": l["seq"],
            "date_local": l["date_local"],
            "flight_no": l["flight_no"],
            "callsign": l["callsign"],
            "from": l["dep_icao"],
            "from_iata": l["dep_iata"],
            "from_name": l["dep_name"],
            "from_city": cities.get(l["dep_icao"]),
            "from_lat": l["dep_lat"],
            "from_lon": l["dep_lon"],
            "to": l["arr_icao"],
            "to_iata": l["arr_iata"],
            "to_name": l["arr_name"],
            "to_city": cities.get(l["arr_icao"]),
            "to_lat": l["arr_lat"],
            "to_lon": l["arr_lon"],
            "dep_local": l["dep_local"],
            "arr_local": l["arr_local"],
            "dep_utc": l["dep_utc"],
            "arr_utc": l["arr_utc"],
            "reg": l["reg"],
            "ac_type": l["ac_type"],
            "ac_model": l["ac_model"],
            "ac_age": l["ac_age"],
            "ac_built": l["ac_built"],
            "resolved": bool(l["resolved"]),
            "manual": bool(l["manual"]),
            "unverified": bool(l.get("unverified")),
            "live_state": _effective_live_state(l, fs, now_dt),
            "flight_status": fs.get("state"),
            "dep_utc_est": fs.get("dep_utc_est"),
            "arr_utc_est": fs.get("arr_utc_est"),
            "fa_url": fa_url(l["callsign"], l["flight_no"]),
        }
        (out if l["direction"] == "out" else ret).append(item)
    out.sort(key=lambda x: x["seq"])
    ret.sort(key=lambda x: x["seq"])
    return {
        "id": trip_row["id"],
        "owner_id": trip_row["owner_id"],
        "owner_name": trip_row["owner_name"],
        "out": out,
        "ret": ret,
    }


def active_trips_sync() -> list[dict]:
    """Active trips (ends_at is NOT NULL, filtered in SQL on the indexed
    column), with legs and airport cities fetched in bulk, not per trip."""
    cutoff = _active_cutoff()
    with db.get_conn() as conn:
        trips = conn.execute(
            "SELECT * FROM trips WHERE ends_at >= ? ORDER BY created_at DESC, id DESC",
            (cutoff,),
        ).fetchall()
        ids = [t["id"] for t in trips]
        legs_by_trip: dict[int, list] = {i: [] for i in ids}
        codes: set[str] = set()
        for chunk in _chunks(ids, 500):
            ph = ",".join("?" * len(chunk))
            for l in conn.execute(f"SELECT * FROM legs WHERE trip_id IN ({ph})", chunk).fetchall():
                legs_by_trip[l["trip_id"]].append(l)
                codes.update(c for c in (l["dep_icao"], l["arr_icao"]) if c)
        cities: dict[str, str] = {}
        for chunk in _chunks(sorted(codes), 500):
            ph = ",".join("?" * len(chunk))
            for a in conn.execute(
                f"SELECT ident, municipality FROM airports WHERE ident IN ({ph})", chunk
            ).fetchall():
                if a["municipality"]:
                    cities[a["ident"]] = a["municipality"]
    return [shape_trip(dict(t), legs_by_trip[t["id"]], cities) for t in trips]


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


async def active_trips() -> list[dict]:
    import asyncio

    return await asyncio.to_thread(active_trips_sync)


def purge_old_trips() -> int:
    """Delete trips that ended more than TRIP_PURGE_DAYS ago (legs cascade).
    Returns rows removed; no-op when TRIP_PURGE_DAYS is 0."""
    if config.TRIP_PURGE_DAYS <= 0:
        return 0
    cutoff = now() - config.TRIP_PURGE_DAYS * _DAY
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM trips WHERE ends_at < ?", (cutoff,))
        return cur.rowcount
