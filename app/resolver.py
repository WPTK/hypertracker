"""Turn a user-entered (flight number, date) — or manual airports — into a fully
populated leg: route, scheduled times, callsign, tail number, aircraft type, age.

Everything resolved here is cached at the AeroDataBox layer, so re-entering the
same flight on the same date costs nothing.
"""
import re
import datetime as dt
from . import aerodatabox, db


def normalize_flight_no(s: str) -> str:
    return re.sub(r"[\s\-]", "", (s or "")).upper()


def parse_adb_dt(value: str | None):
    """Parse an AeroDataBox time string like '2026-06-04 12:00Z' to aware UTC dt."""
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    v = v.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _parse_build_date(raw: str):
    for length, fmt in ((10, "%Y-%m-%d"), (7, "%Y-%m"), (4, "%Y")):
        try:
            return dt.datetime.strptime(raw[:length], fmt)
        except Exception:
            continue
    return None


def _age_years(rec: dict):
    """Best-effort aircraft age in years (1 dp) from whatever date AeroDataBox has."""
    for field in ("firstFlightDate", "rolloutDate", "deliveryDate",
                  "registrationDate", "productionDate"):
        raw = rec.get(field)
        if not raw:
            continue
        built = _parse_build_date(str(raw))
        if built:
            days = (dt.datetime.utcnow() - built).days
            return round(days / 365.25, 1), str(raw)[:10]
    return None, None


def _airport_fields(code_icao: str, code_iata: str, loc: dict | None):
    """Prefer the local OurAirports table for coordinates/name; fall back to ADB."""
    ap = db.find_airport(code_icao) or db.find_airport(code_iata)
    if ap:
        return ap["ident"], ap.get("iata"), ap.get("name"), ap.get("lat"), ap.get("lon")
    lat = lon = None
    if loc:
        lat, lon = loc.get("lat"), loc.get("lon")
    return code_icao, code_iata, None, lat, lon


async def resolve_leg(direction: str, seq: int, flight_no: str, date_local: str,
                      manual_from: str | None = None, manual_to: str | None = None) -> dict:
    flight_no = normalize_flight_no(flight_no)
    leg = {
        "direction": direction, "seq": seq, "date_local": date_local,
        "flight_no": flight_no, "callsign": None,
        "dep_icao": None, "dep_iata": None, "dep_name": None, "dep_lat": None, "dep_lon": None,
        "dep_local": None, "dep_utc": None,
        "arr_icao": None, "arr_iata": None, "arr_name": None, "arr_lat": None, "arr_lon": None,
        "arr_local": None, "arr_utc": None,
        "reg": None, "ac_type": None, "ac_model": None, "ac_age": None, "ac_built": None,
        "resolved": 0, "manual": 0,
    }

    # Manual airports take priority and skip the API entirely (zero cost path).
    if manual_from or manual_to:
        leg["manual"] = 1
        a = db.find_airport(manual_from) if manual_from else None
        b = db.find_airport(manual_to) if manual_to else None
        if a:
            leg.update(dep_icao=a["ident"], dep_iata=a.get("iata"), dep_name=a.get("name"),
                       dep_lat=a.get("lat"), dep_lon=a.get("lon"))
        if b:
            leg.update(arr_icao=b["ident"], arr_iata=b.get("iata"), arr_name=b.get("name"),
                       arr_lat=b.get("lat"), arr_lon=b.get("lon"))
        return leg

    f = await aerodatabox.flight_by_number(flight_no, date_local)
    if not f:
        return leg  # unresolved; UI will prompt for manual airports

    leg["resolved"] = 1
    dep = f.get("departure") or {}
    arr = f.get("arrival") or {}
    dep_ap = dep.get("airport") or {}
    arr_ap = arr.get("airport") or {}

    di, dia, dn, dla, dlo = _airport_fields(dep_ap.get("icao"), dep_ap.get("iata"), dep_ap.get("location"))
    ai, aia, an, ala, alo = _airport_fields(arr_ap.get("icao"), arr_ap.get("iata"), arr_ap.get("location"))
    leg.update(dep_icao=di, dep_iata=dia, dep_name=dn or dep_ap.get("name"), dep_lat=dla, dep_lon=dlo,
               arr_icao=ai, arr_iata=aia, arr_name=an or arr_ap.get("name"), arr_lat=ala, arr_lon=alo)

    leg["dep_local"] = (dep.get("scheduledTime") or {}).get("local")
    leg["dep_utc"] = (dep.get("scheduledTime") or {}).get("utc")
    leg["arr_local"] = (arr.get("scheduledTime") or {}).get("local")
    leg["arr_utc"] = (arr.get("scheduledTime") or {}).get("utc")

    leg["callsign"] = f.get("callSign") or _derive_callsign(f, flight_no)

    ac = f.get("aircraft") or {}
    leg["reg"] = (ac.get("reg") or "").upper() or None
    leg["ac_model"] = ac.get("model")

    # Enrich with type + age from the aircraft endpoint (cached per tail).
    if leg["reg"]:
        rec = await aerodatabox.aircraft_by_reg(leg["reg"])
        if rec:
            leg["ac_model"] = rec.get("model") or rec.get("typeName") or leg["ac_model"]
            leg["ac_type"] = rec.get("typeCode") or rec.get("icaoCode") or leg["ac_type"]
            age, built = _age_years(rec)
            leg["ac_age"] = age
            leg["ac_built"] = built
    return leg


def _derive_callsign(f: dict, flight_no: str) -> str | None:
    icao = ((f.get("airline") or {}).get("icao") or "").upper()
    digits = re.sub(r"\D", "", flight_no)
    if icao and digits:
        return icao + digits
    return flight_no or None
