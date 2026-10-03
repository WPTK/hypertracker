"""Lifecycle (when a trip drops off the board), shaping trips for the API, and
purging long-dead rows."""
import asyncio
import datetime as dt
import time
from . import config, db
from .resolver import parse_adb_dt
from .airplaneslive import is_airborne, globe_url


def compute_ends_at(legs: list[dict]) -> int | None:
    """Epoch seconds of the latest scheduled arrival across legs, or None when
    no leg has a parseable arrival (such trips are always shown so the owner
    can fix or remove them)."""
    arrivals = [parse_adb_dt(l.get("arr_utc")) for l in legs]
    arrivals = [a for a in arrivals if a]
    return int(max(arrivals).timestamp()) if arrivals else None


def _active_cutoff() -> int:
    """A trip is active until TRIP_GRACE_HOURS after its final arrival."""
    return int(time.time()) - config.TRIP_GRACE_HOURS * 3600


def fa_url(callsign: str | None, flight_no: str | None) -> str:
    ident = callsign or flight_no or ""
    return f"https://flightaware.com/live/flight/{ident}"


async def shape_trip(trip_row: dict, leg_rows: list[dict]) -> dict:
    legs = [dict(r) for r in leg_rows]
    now = dt.datetime.now(dt.timezone.utc)

    # Probe airplanes.live only for legs plausibly in the air right now, and do
    # the probes concurrently (each is independent and 60s-cached; the client
    # module enforces the API's 1 req/sec courtesy on cache misses).
    probe_idx = []
    for i, l in enumerate(legs):
        dep = parse_adb_dt(l.get("dep_utc"))
        arr = parse_adb_dt(l.get("arr_utc"))
        if dep and arr and (dep - dt.timedelta(minutes=20)) <= now <= (arr + dt.timedelta(minutes=45)):
            probe_idx.append(i)
    results = await asyncio.gather(
        *[is_airborne(legs[i].get("callsign")) for i in probe_idx]
    ) if probe_idx else []
    live_map = dict(zip(probe_idx, results))

    out, ret = [], []
    for i, l in enumerate(legs):
        live = bool(live_map.get(i))
        item = {
            "direction": l["direction"], "seq": l["seq"],
            "date_local": l["date_local"], "flight_no": l["flight_no"],
            "callsign": l["callsign"],
            "from": l["dep_icao"], "from_iata": l["dep_iata"], "from_name": l["dep_name"],
            "from_lat": l["dep_lat"], "from_lon": l["dep_lon"],
            "to": l["arr_icao"], "to_iata": l["arr_iata"], "to_name": l["arr_name"],
            "to_lat": l["arr_lat"], "to_lon": l["arr_lon"],
            "dep_local": l["dep_local"], "arr_local": l["arr_local"],
            "reg": l["reg"], "ac_type": l["ac_type"], "ac_model": l["ac_model"],
            "ac_age": l["ac_age"], "ac_built": l["ac_built"],
            "resolved": bool(l["resolved"]), "manual": bool(l["manual"]),
            "live": live,
            "fa_url": fa_url(l["callsign"], l["flight_no"]),
            "live_url": globe_url(l["callsign"] or l["flight_no"] or ""),
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


async def active_trips() -> list[dict]:
    """Active trips, filtered in SQL on the indexed ends_at column (NULL =
    unknown end = always shown), with legs fetched in one query instead of N."""
    cutoff = _active_cutoff()
    with db.get_conn() as conn:
        trips = conn.execute(
            "SELECT * FROM trips WHERE ends_at IS NULL OR ends_at >= ? "
            "ORDER BY created_at DESC", (cutoff,),
        ).fetchall()
        ids = [t["id"] for t in trips]
        legs_by_trip: dict[int, list] = {i: [] for i in ids}
        if ids:
            ph = ",".join("?" * len(ids))
            for l in conn.execute(f"SELECT * FROM legs WHERE trip_id IN ({ph})", ids).fetchall():
                legs_by_trip[l["trip_id"]].append(l)
    shaped = await asyncio.gather(
        *[shape_trip(dict(t), legs_by_trip[t["id"]]) for t in trips]
    )
    return list(shaped)


def purge_old_trips() -> int:
    """Delete trips that left the board more than TRIP_PURGE_DAYS ago (their
    legs cascade). Returns rows removed; no-op when TRIP_PURGE_DAYS is 0.
    Trips with ends_at NULL are never purged — they're shown until removed."""
    if config.TRIP_PURGE_DAYS <= 0:
        return 0
    cutoff = _active_cutoff() - config.TRIP_PURGE_DAYS * 86400
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM trips WHERE ends_at IS NOT NULL AND ends_at < ?", (cutoff,)
        )
        return cur.rowcount
