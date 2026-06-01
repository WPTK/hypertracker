"""AeroDataBox client.

Two calls matter for us:
  1. Flight status by number + date  -> route, scheduled times, callsign, tail number
  2. Aircraft by registration        -> type + build date (= age)

Both are cached in SQLite (see db.cache_*). Networking is best-effort: any failure
returns None and the caller falls back to manual entry / partial data.

NOTE: AeroDataBox response shapes vary slightly by marketplace/version and by
aircraft. Parsing here is deliberately defensive (lots of .get / fallbacks). If
you change marketplaces, eyeball one live response and adjust field names.
"""
import httpx
from . import config, db


def _headers():
    h = {"Accept": "application/json"}
    if config.AERODATABOX_KEY:
        # RapidAPI-style auth. API.Market uses x-magicapi-key / x-api-market-key;
        # set AERODATABOX_KEY and adjust here if you switch marketplaces.
        h["x-rapidapi-key"] = config.AERODATABOX_KEY
        h["x-rapidapi-host"] = config.AERODATABOX_HOST
    return h


async def _get(path: str, params: dict | None = None):
    url = config.AERODATABOX_BASE.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=_headers(), params=params or {})
        if r.status_code == 200:
            return r.json()
        return {"_error": r.status_code, "_body": r.text[:300]}
    except Exception as e:  # network, timeout, json
        return {"_error": "exception", "_body": str(e)[:300]}


async def flight_by_number(flight_no: str, date_local: str) -> dict | None:
    """Return the raw AeroDataBox flight payload for flight_no on date_local.

    date_local is interpreted as the DEPARTURE airport's local date, which is how
    AeroDataBox indexes flights and how schedules are published.
    """
    key = f"{flight_no}|{date_local}"
    cached = db.cache_get("flight_cache", "cache_key", key, config.FLIGHT_CACHE_TTL)
    if cached is not None:
        return cached

    data = await _get(
        f"/flights/number/{flight_no}/{date_local}",
        params={
            "withAircraftImage": "false",
            "withLocation": "true",       # include airport lat/lon
            "dateLocalRole": "Departure",  # disambiguate overnight flights
        },
    )
    if isinstance(data, dict) and data.get("_error"):
        return None
    # Endpoint may return a list of movements or a dict wrapping one.
    flights = data if isinstance(data, list) else data.get("flights") or [data]
    if not flights:
        return None
    chosen = _pick_flight(flights, date_local)
    if chosen:
        db.cache_put("flight_cache", "cache_key", key, chosen)
    return chosen


def _pick_flight(flights: list, date_local: str) -> dict | None:
    """Pick the movement whose departure local date matches the requested date."""
    for f in flights:
        dep = (f.get("departure") or {}).get("scheduledTime") or {}
        local = (dep.get("local") or "")[:10]
        if local == date_local:
            return f
    return flights[0] if flights else None


async def aircraft_by_reg(reg: str) -> dict | None:
    if not reg:
        return None
    reg = reg.strip().upper()
    cached = db.cache_get("aircraft_cache", "reg", reg, config.AIRCRAFT_CACHE_TTL)
    if cached is not None:
        return cached
    data = await _get(f"/aircrafts/reg/{reg}")
    if isinstance(data, dict) and data.get("_error"):
        return None
    # Some plans return a list; normalise to the first record.
    rec = data[0] if isinstance(data, list) and data else data
    if isinstance(rec, dict) and rec and not rec.get("_error"):
        db.cache_put("aircraft_cache", "reg", reg, rec)
        return rec
    return None
