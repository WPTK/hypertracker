"""Turn user rows (flight number + date, or manual airports) into populated legs.

`resolve_rows` is the entry point. It never raises for upstream trouble: every
row comes back with a `status` (see the API contract), a friendly `message`,
`candidates` (for ambiguous / route-mismatch answers) and the DB leg columns.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

from . import aerodatabox as adb
from . import config, db
from .aerodatabox import parse_adb_dt  # noqa: F401  (public; lifecycle imports it)
from .validation import (  # noqa: F401  (normalize_flight_no is re-exported)
    normalize_airport_code,
    normalize_flight_no,
    validate_leg_date,
)

log = logging.getLogger("hypertracker.resolver")

_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")

# Keys refresh_leg never reports as changes.
_IDENTITY_KEYS = {"direction", "seq", "date_local", "flight_no"}


def _knob(name: str, default):
    return getattr(config, name, default)


# --- messages ----------------------------------------------------------------
def _pretty_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d:%b} {d.day}"
    except ValueError:
        return iso


def _label(iata, icao) -> str:
    return iata or icao or "?"


def msg_found(flight_no, dep, arr) -> str:
    return f"{flight_no}: {dep} to {arr}."


def msg_manual(dep, arr) -> str:
    return f"Using {dep} to {arr}."


def msg_airport_unknown(code) -> str:
    return f"Unknown airport {code}."


def msg_not_found(flight_no, date_iso, other_dates=()) -> str:
    when = _pretty_date(date_iso)
    if other_dates:
        days = " or ".join(_pretty_date(d) for d in other_dates[:2])
        return (
            f"{flight_no} is not scheduled on {when}, but is on {days}. "
            "Check the date, or add the airports yourself."
        )
    return f"{flight_no} not found on {when}. Check the number and date, or add the airports yourself."


def msg_route_mismatch(flight_no, date_iso) -> str:
    return (
        f"{flight_no} is scheduled on {_pretty_date(date_iso)}, but not on that route. "
        "Check the airports, or leave them blank."
    )


def msg_ambiguous(flight_no, date_iso, n) -> str:
    return f"{flight_no} flies {n} legs on {_pretty_date(date_iso)}. Pick the one you're taking."


MSG_BAD_KEY = (
    "Flight lookup isn't set up correctly right now. "
    "Please let the person who runs this board know. "
    "You can still add the airports yourself."
)
MSG_QUOTA = "Flight lookups are rate limited right now. Try again in a minute, or add the airports yourself."
MSG_DOWN = "The flight data service is not responding. Try again in a minute, or add the airports yourself."
MSG_DEADLINE = "The lookup timed out. Try again in a moment, or add the airports yourself."
MSG_INCOMPLETE = "That flight came back without a full route. Add the airports yourself."
MSG_NEED_AIRPORTS = "Add a flight number, or both airports."


# --- dates -------------------------------------------------------------------
def check_date(raw, today: dt.date | None = None):
    """-> (iso | None, status | None, message | None).

    A missing date means today (UTC). Garbage is 'invalid'; outside the
    FLIGHT_WINDOW_* knobs is 'out_of_window'."""
    today = today or dt.datetime.now(dt.UTC).date()
    iso, err = validate_leg_date(
        raw, today, int(_knob("FLIGHT_WINDOW_PAST_DAYS", 2)), int(_knob("FLIGHT_WINDOW_FUTURE_DAYS", 330))
    )
    if err is None:
        return iso, None, None
    s = str(raw or "").strip()
    try:
        dt.date.fromisoformat(s[:10])
    except ValueError:
        return None, "invalid", err
    return None, "out_of_window", err


# --- leg construction --------------------------------------------------------
def _blank_leg(direction, seq, flight_no, date_local) -> dict:
    return {
        "direction": direction,
        "seq": seq,
        "date_local": date_local,
        "flight_no": flight_no or None,
        "callsign": None,
        "dep_icao": None,
        "dep_iata": None,
        "dep_name": None,
        "dep_lat": None,
        "dep_lon": None,
        "dep_local": None,
        "dep_utc": None,
        "arr_icao": None,
        "arr_iata": None,
        "arr_name": None,
        "arr_lat": None,
        "arr_lon": None,
        "arr_local": None,
        "arr_utc": None,
        "reg": None,
        "ac_type": None,
        "ac_model": None,
        "ac_age": None,
        "ac_built": None,
        "resolved": 0,
        "manual": 0,
        # Extras (not DB columns; callers may ignore them).
        "flight_status": None,
        "dep_revised_utc": None,
        "dep_revised_local": None,
        "arr_revised_utc": None,
        "arr_revised_local": None,
        "dep_city": None,
        "arr_city": None,
    }


def _result(leg: dict, status: str, message: str, candidates=None, unverified=False) -> dict:
    out = dict(leg)
    out.update(status=status, message=message, candidates=candidates or [], unverified=unverified)
    return out


def _set_airport(leg: dict, side: str, ap: dict) -> None:
    leg.update(
        {
            f"{side}_icao": ap["ident"],
            f"{side}_iata": ap.get("iata"),
            f"{side}_name": ap.get("name"),
            f"{side}_lat": ap.get("lat"),
            f"{side}_lon": ap.get("lon"),
        }
    )


def _fill_manual(leg: dict, dep_code, arr_code) -> tuple[bool, list]:
    """Fill the route from airports we know. Returns (any_filled, unknown_codes)."""
    unknown, filled = [], False
    for side, code in (("dep", dep_code), ("arr", arr_code)):
        if not code:
            continue
        ap = db.find_airport(code)
        if ap:
            _set_airport(leg, side, ap)
            filled = True
        else:
            unknown.append(code)
    if filled:
        leg["manual"] = 1
    return filled, unknown


def _adb_airport(side: dict) -> dict | None:
    """Airport dict (ident/iata/name/lat/lon) from the local table, falling back
    to what AeroDataBox sent. None when the flight names no airport at all."""
    ap = (side or {}).get("airport") or {}
    icao = str(ap.get("icao") or "").upper() or None
    iata = str(ap.get("iata") or "").upper() or None
    row = (db.find_airport(icao) if icao else None) or (db.find_airport(iata) if iata else None)
    if row:
        return row
    if not (icao or iata):
        return None
    loc = ap.get("location") if isinstance(ap.get("location"), dict) else {}
    return {
        "ident": icao or iata,
        "iata": iata,
        "name": ap.get("name"),
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
    }


def derive_callsign(f: dict, flight_no: str) -> str | None:
    """API callSign, else ICAO airline code + the flight number's digits, else
    None. A raw (IATA) flight number is never used as a callsign."""
    cs = str(f.get("callSign") or "").strip().upper()
    if _CALLSIGN_RE.match(cs):
        return cs
    icao = str((f.get("airline") or {}).get("icao") or "").strip().upper()
    digits = re.sub(r"\D", "", flight_no or "")
    cand = icao + digits
    if re.fullmatch(r"[A-Z]{3}", icao) and digits and _CALLSIGN_RE.match(cand):
        return cand
    return None


def _candidate(f: dict) -> dict:
    dep, arr = f.get("departure") or {}, f.get("arrival") or {}
    da, aa = _adb_airport(dep), _adb_airport(arr)
    return {
        "from": da["ident"] if da else None,
        "from_iata": da.get("iata") if da else None,
        "to": aa["ident"] if aa else None,
        "to_iata": aa.get("iata") if aa else None,
        "dep_local": (dep.get("scheduledTime") or {}).get("local"),
        "arr_local": (arr.get("scheduledTime") or {}).get("local"),
    }


def _apply_flight(leg: dict, f: dict, flight_no: str) -> bool:
    """Copy a picked AeroDataBox flight into `leg`. True when both airports exist."""
    dep, arr = f.get("departure") or {}, f.get("arrival") or {}
    da, aa = _adb_airport(dep), _adb_airport(arr)
    if not (da and aa):
        return False
    _set_airport(leg, "dep", da)
    _set_airport(leg, "arr", aa)
    leg["dep_local"] = (dep.get("scheduledTime") or {}).get("local")
    leg["dep_utc"] = (dep.get("scheduledTime") or {}).get("utc")
    leg["arr_local"] = (arr.get("scheduledTime") or {}).get("local")
    leg["arr_utc"] = (arr.get("scheduledTime") or {}).get("utc")
    leg["callsign"] = derive_callsign(f, flight_no)
    status = f.get("status")
    leg["flight_status"] = status if isinstance(status, str) else None
    for side, m in (("dep", dep), ("arr", arr)):
        rev = m.get("revisedTime") or {}
        leg[f"{side}_revised_utc"] = rev.get("utc")
        leg[f"{side}_revised_local"] = rev.get("local")
        ap = m.get("airport") or {}
        leg[f"{side}_city"] = ap.get("municipalityName") or None
    ac = f.get("aircraft") if isinstance(f.get("aircraft"), dict) else {}
    reg = str(ac.get("reg") or "").strip().upper()
    leg["reg"] = reg or None
    model = ac.get("model")
    leg["ac_model"] = model if isinstance(model, str) and model.strip() else None
    leg["resolved"] = 1
    return True


async def _enrich_aircraft(leg: dict) -> None:
    if not leg.get("reg"):
        return
    rec = await adb.aircraft_by_reg(leg["reg"])
    if not rec:
        return
    m = adb.map_aircraft(rec)
    leg["ac_type"] = m["ac_type"] or leg["ac_type"]
    # Full type name wins; the short model code ('A359') must not replace a
    # good flight-level model name.
    full = rec.get("typeName") if isinstance(rec.get("typeName"), str) and rec["typeName"].strip() else None
    leg["ac_model"] = full or leg["ac_model"] or m["ac_model"]
    leg["ac_age"], leg["ac_built"] = m["ac_age"], m["ac_built"]


# --- single-row resolution ---------------------------------------------------
async def _resolve_one(row: dict, *, use_cache: bool = True) -> dict:
    direction = row.get("direction") or "out"
    seq = int(row.get("seq") or 0)
    flight_no = normalize_flight_no(row.get("flight_no"))
    dep_code = normalize_airport_code(row.get("from"))
    arr_code = normalize_airport_code(row.get("to"))
    date_iso, bad, msg = await asyncio.to_thread(check_date, row.get("date"))
    leg = _blank_leg(direction, seq, flight_no, date_iso or str(row.get("date") or "") or None)
    if bad:
        return _result(leg, bad, msg)

    # Manual row: both airports, no network.
    if not flight_no:
        if not (dep_code and arr_code):
            return _result(leg, "invalid", MSG_NEED_AIRPORTS)
        _, unknown = await asyncio.to_thread(_fill_manual, leg, dep_code, arr_code)
        if unknown:
            return _result(leg, "airport_unknown", msg_airport_unknown(unknown[0]))
        return _result(
            leg,
            "manual_ok",
            msg_manual(_label(leg["dep_iata"], leg["dep_icao"]), _label(leg["arr_iata"], leg["arr_icao"])),
        )

    async def unresolved(status: str, message: str, candidates=None) -> dict:
        # User-supplied From/To fill the manual route but never flip `resolved`.
        await asyncio.to_thread(_fill_manual, leg, dep_code, arr_code)
        return _result(leg, status, message, candidates)

    res, pick = await adb.flight_by_number(flight_no, date_iso, dep_code, arr_code, use_cache=use_cache)
    if res.kind == adb.KIND_BAD_KEY:
        return await unresolved("upstream_unavailable", MSG_BAD_KEY)
    if res.kind == adb.KIND_QUOTA:
        return await unresolved("quota", MSG_QUOTA)
    if res.kind in (adb.KIND_DOWN, adb.KIND_BAD_PAYLOAD):
        return await unresolved("upstream_unavailable", MSG_DOWN)
    if res.kind == adb.KIND_NO_FLIGHTS or pick is None:
        return await unresolved("not_found", msg_not_found(flight_no, date_iso))

    if pick.kind == "not_found":
        if pick.reason == "route_mismatch":
            return await unresolved(
                "not_found",
                msg_route_mismatch(flight_no, date_iso),
                [await asyncio.to_thread(_candidate, f) for f in pick.candidates],
            )
        return await unresolved("not_found", msg_not_found(flight_no, date_iso, pick.other_dates))
    if pick.kind == "ambiguous":
        cands = await asyncio.to_thread(lambda: [_candidate(f) for f in pick.candidates])
        return _result(leg, "ambiguous", msg_ambiguous(flight_no, date_iso, len(cands)), cands)

    ok = await asyncio.to_thread(_apply_flight, leg, pick.flight, flight_no)
    if not ok:
        return await unresolved("not_found", MSG_INCOMPLETE)
    await _enrich_aircraft(leg)
    return _result(
        leg,
        "ok",
        msg_found(
            flight_no, _label(leg["dep_iata"], leg["dep_icao"]), _label(leg["arr_iata"], leg["arr_icao"])
        ),
    )


def _failed_row(row: dict, status: str, message: str) -> dict:
    flight_no = normalize_flight_no(row.get("flight_no"))
    leg = _blank_leg(
        row.get("direction") or "out", int(row.get("seq") or 0), flight_no, str(row.get("date") or "") or None
    )
    try:
        _fill_manual(leg, normalize_airport_code(row.get("from")), normalize_airport_code(row.get("to")))
    except Exception:
        log.debug("could not fill the manual route hint", exc_info=True)
    return _result(leg, status, message)


async def resolve_rows(rows: list[dict], *, deadline: float | None = None) -> list[dict]:
    """Resolve rows concurrently (RESOLVE_CONCURRENCY at a time) within `deadline`
    seconds overall. Order is preserved. Rows that miss the deadline, or blow up,
    come back as `upstream_unavailable`; this function does not raise for them."""
    if deadline is None:
        deadline = float(_knob("RESOLVE_DEADLINE", 20))
    if not rows:
        return []
    sem = asyncio.Semaphore(max(1, int(_knob("RESOLVE_CONCURRENCY", 3))))

    async def run(row):
        async with sem:
            return await _resolve_one(row)

    tasks = [asyncio.ensure_future(run(r)) for r in rows]
    try:
        await asyncio.wait(tasks, timeout=deadline)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    out = []
    for row, t in zip(rows, tasks, strict=False):
        if t.cancelled() or not t.done():
            out.append(_failed_row(row, "upstream_unavailable", MSG_DEADLINE))
        elif t.exception() is not None:
            log.error("resolving %r failed", row.get("flight_no"), exc_info=t.exception())
            out.append(_failed_row(row, "upstream_unavailable", MSG_DOWN))
        else:
            out.append(t.result())
    # Let cancelled tasks finish unwinding so nothing is left pending.
    await asyncio.gather(*tasks, return_exceptions=True)
    return out


async def refresh_leg(leg: dict) -> dict | None:
    """Re-resolve a stored flight leg bypassing the flight cache. Returns only the
    DB columns that changed (never overwriting a value with None), or None when
    the leg can't be refreshed or nothing changed."""
    flight_no = normalize_flight_no(leg.get("flight_no"))
    date_local = leg.get("date_local")
    if not flight_no or not date_local:
        return None
    row = {
        "direction": leg.get("direction"),
        "seq": leg.get("seq"),
        "flight_no": flight_no,
        "date": date_local,
        "from": leg.get("dep_icao"),
        "to": leg.get("arr_icao"),
    }
    try:
        new = await _resolve_one(row, use_cache=False)
    except Exception:
        log.exception("refresh_leg failed for %s", flight_no)
        return None
    if new["status"] != "ok":
        return None
    changed = {
        k: v
        for k, v in new.items()
        if k in leg and k not in _IDENTITY_KEYS and v is not None and v != leg.get(k)
    }
    return changed or None


# --- legacy wrapper ----------------------------------------------------------
_EXTRA_KEYS = ("status", "message", "candidates", "unverified")


async def resolve_leg(
    direction: str,
    seq: int,
    flight_no: str,
    date_local: str,
    manual_from: str | None = None,
    manual_to: str | None = None,
) -> dict:
    """Old single-leg entry point: DB columns only. Prefer resolve_rows."""
    if not normalize_flight_no(flight_no):  # old behaviour: manual legs skip the window
        leg = _blank_leg(direction, seq, None, date_local)
        if manual_from or manual_to:
            _fill_manual(leg, normalize_airport_code(manual_from), normalize_airport_code(manual_to))
        return leg
    out = (
        await resolve_rows(
            [
                {
                    "direction": direction,
                    "seq": seq,
                    "flight_no": flight_no,
                    "date": date_local,
                    "from": manual_from,
                    "to": manual_to,
                }
            ]
        )
    )[0]
    return {k: v for k, v in out.items() if k not in _EXTRA_KEYS}
