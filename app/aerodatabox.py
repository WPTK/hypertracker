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


async def flight_by_number(flight_no: str, date_local: str,
                           dep_hint: str | None = None, arr_hint: str | None = None) -> dict | None:
    """Return one AeroDataBox flight leg for flight_no on date_local.

    date_local is the DEPARTURE airport's local date. The endpoint returns every
    leg flown under this number that day (carriers reuse a number across a
    multi-leg rotation), so dep_hint/arr_hint (ICAO or IATA) pick the leg the
    caller means. The whole day's list is cached; selection happens per call, so
    the same number resolves correctly for different legs/hints.
    """
    key = f"v2|{flight_no}|{date_local}"
    flights = db.cache_get("flight_cache", "cache_key", key, config.FLIGHT_CACHE_TTL)
    if flights is None:
        data = await _get(
            f"/flights/number/{flight_no}/{date_local}",
            params={
                "withAircraftImage": "false",
                "withLocation": "true",        # include airport lat/lon
                "dateLocalRole": "Departure",   # disambiguate overnight flights
            },
        )
        if isinstance(data, dict) and data.get("_error"):
            return None
        # Endpoint may return a list of movements or a dict wrapping one.
        flights = data if isinstance(data, list) else (data.get("flights") or [data])
        if not flights:
            return None
        db.cache_put("flight_cache", "cache_key", key, flights)
    return _pick_flight(flights, date_local, dep_hint, arr_hint)


def _dep_local_date(f: dict) -> str:
    return (((f.get("departure") or {}).get("scheduledTime") or {}).get("local") or "")[:10]


def _leg_matches(f: dict, dep_hint: str | None, arr_hint: str | None) -> bool:
    """True if the leg's departure/arrival match the given hints (ICAO or IATA)."""
    def codes(side: str) -> set:
        ap = (f.get(side) or {}).get("airport") or {}
        return {(ap.get("icao") or "").upper(), (ap.get("iata") or "").upper()} - {""}
    if dep_hint and dep_hint not in codes("departure"):
        return False
    if arr_hint and arr_hint not in codes("arrival"):
        return False
    return True


def _pick_flight(flights: list, date_local: str,
                 dep_hint: str | None = None, arr_hint: str | None = None) -> dict | None:
    """Choose one leg. Prefer legs departing on the requested date; when a number
    flew several that day, use the From/To hints to pin the exact one."""
    dep_hint = (dep_hint or "").strip().upper() or None
    arr_hint = (arr_hint or "").strip().upper() or None
    pool = [f for f in flights if _dep_local_date(f) == date_local] or list(flights)
    if len(pool) <= 1:
        return pool[0] if pool else None          # unambiguous — hints not needed
    if dep_hint or arr_hint:
        for f in pool:
            if _leg_matches(f, dep_hint, arr_hint):
                return f
        return None                                # hints given but none matched
    return pool[0]                                 # ambiguous, no hint: best effort


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
