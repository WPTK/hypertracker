"""Lifecycle (when a trip drops off the board) and shaping trips for the API."""
import datetime as dt
from . import config, db
from .resolver import parse_adb_dt
from .airplaneslive import is_airborne, globe_url


def _trip_end_utc(legs: list[dict]):
    """Latest scheduled arrival across all legs, as aware UTC datetime."""
    arrivals = [parse_adb_dt(l.get("arr_utc")) for l in legs]
    arrivals = [a for a in arrivals if a]
    return max(arrivals) if arrivals else None


def is_active(legs: list[dict]) -> bool:
    """A trip stays until TRIP_GRACE_HOURS after its final scheduled arrival.

    Trips we couldn't resolve (no times) are always shown — the owner can clean
    them up manually.
    """
    end = _trip_end_utc(legs)
    if end is None:
        return True
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=config.TRIP_GRACE_HOURS)
    return end >= cutoff


def fa_url(callsign: str | None, flight_no: str | None) -> str:
    ident = callsign or flight_no or ""
    return f"https://flightaware.com/live/flight/{ident}"


async def shape_trip(trip_row: dict, leg_rows: list[dict]) -> dict:
    legs = [dict(r) for r in leg_rows]
    out, ret = [], []
    now = dt.datetime.now(dt.timezone.utc)

    for l in legs:
        dep = parse_adb_dt(l.get("dep_utc"))
        arr = parse_adb_dt(l.get("arr_utc"))
        # Only probe airplanes.live for legs plausibly in the air right now.
        live = False
        if dep and arr and (dep - dt.timedelta(minutes=20)) <= now <= (arr + dt.timedelta(minutes=45)):
            live = await is_airborne(l.get("callsign"))
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
    with db.get_conn() as conn:
        trips = conn.execute("SELECT * FROM trips ORDER BY created_at DESC").fetchall()
        result = []
        for t in trips:
            legs = conn.execute(
                "SELECT * FROM legs WHERE trip_id = ?", (t["id"],)
            ).fetchall()
            if not is_active([dict(l) for l in legs]):
                continue
            result.append((t, legs))
    shaped = []
    for t, legs in result:
        shaped.append(await shape_trip(dict(t), legs))
    return shaped
