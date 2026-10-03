"""Flight status from AeroDataBox, for legs that airplanes.live cannot see.

Polar and oceanic stretches have almost no ADS-B feeder coverage, so a flight that is
plainly in the air can have no sighting for hours. AeroDataBox knows the status
(EnRoute, Arrived, Canceled, ...) and the revised and actual times, so the board uses it
as a fallback. In memory only: a restart simply fetches again.

Request handlers only call `get`, which never touches the network. One background task
(`run_refresher`) fetches, and spaces its calls out: the RapidAPI BASIC plan is small.
"""

import asyncio
import datetime as dt
import logging

from . import aerodatabox as adb

log = logging.getLogger("hypertracker.flightstatus")

AIRBORNE = "airborne"
LANDED = "landed"
CANCELLED = "cancelled"
DELAYED = "delayed"

_AIRBORNE_STATUSES = {"EnRoute", "Departed", "Approaching"}
_CANCELLED_STATUSES = {"Canceled", "CanceledUncertain"}

WINDOW_BEFORE = dt.timedelta(hours=3)  # start asking this long before departure
WINDOW_AFTER = dt.timedelta(hours=2)  # and stop this long after arrival
PRE_DEPARTURE_EVERY = 20 * 60
EN_ROUTE_EVERY = 60 * 60
NEAR_ARRIVAL_EVERY = 10 * 60
NEAR_ARRIVAL_WITHIN = 45 * 60
RETRY_AFTER_FAILURE = 10 * 60

_state: dict[tuple[str, str], dict] = {}
_next_at: dict[tuple[str, str], float] = {}


def _key(leg: dict) -> tuple[str, str]:
    return (str(leg.get("flight_no") or "").upper(), str(leg.get("date_local") or ""))


def reset() -> None:
    _state.clear()
    _next_at.clear()


def get(leg: dict) -> dict | None:
    """The last known status of a leg, or None. Memory only."""
    return _state.get(_key(leg))


def _utc(movement: dict, field: str) -> str | None:
    v = (movement.get(field) or {}).get("utc")
    return v if isinstance(v, str) and v else None


def _iata(movement: dict) -> str | None:
    return (movement.get("airport") or {}).get("iata")


def pick(flights: list, leg: dict) -> dict | None:
    """The flight on this leg's route; a lone result is taken as is."""
    for f in flights:
        if _iata(f.get("departure") or {}) == leg.get("from_iata") and _iata(f.get("arrival") or {}) == leg.get("to_iata"):
            return f
    return flights[0] if len(flights) == 1 else None


def summarise(f: dict, now: dt.datetime) -> dict:
    dep, arr = f.get("departure") or {}, f.get("arrival") or {}
    status = str(f.get("status") or "")
    dep_runway, arr_runway = _utc(dep, "runwayTime"), _utc(arr, "runwayTime")
    took_off = False
    if dep_runway:
        t = adb.parse_adb_dt(dep_runway)
        took_off = bool(t and t <= now)
    if status == "Arrived" or arr_runway:
        state = LANDED
    elif status in _CANCELLED_STATUSES:
        state = CANCELLED
    elif status in _AIRBORNE_STATUSES or took_off:
        state = AIRBORNE
    elif status == "Delayed":
        state = DELAYED
    else:
        state = None
    return {
        "state": state,
        "dep_utc_est": dep_runway or _utc(dep, "revisedTime") or _utc(dep, "scheduledTime"),
        "arr_utc_est": arr_runway or _utc(arr, "revisedTime") or _utc(arr, "scheduledTime"),
    }


def _interval(state: str | None, arr_est: str | None, now: dt.datetime) -> int:
    if state == AIRBORNE:
        arr = adb.parse_adb_dt(arr_est)
        if arr and (arr - now).total_seconds() <= NEAR_ARRIVAL_WITHIN:
            return NEAR_ARRIVAL_EVERY
        return EN_ROUTE_EVERY
    return PRE_DEPARTURE_EVERY


def _due(now_ts: float) -> list[dict]:
    from . import lifecycle  # late: lifecycle imports this module

    now = dt.datetime.fromtimestamp(now_ts, dt.UTC)
    out = []
    for trip in lifecycle.active_trips_sync():
        for leg in trip["out"] + trip["ret"]:
            if not leg.get("flight_no") or leg.get("manual"):
                continue
            dep, arr = adb.parse_adb_dt(leg.get("dep_utc")), adb.parse_adb_dt(leg.get("arr_utc"))
            if not (dep and arr) or not (dep - WINDOW_BEFORE <= now <= arr + WINDOW_AFTER):
                continue
            known = _state.get(_key(leg))
            if known and known["state"] in (LANDED, CANCELLED):
                continue
            if _next_at.get(_key(leg), 0) > now_ts:
                continue
            out.append(leg)
    return out


async def refresh_once() -> int:
    """Fetch every leg that is due. Returns how many were fetched."""
    from . import lifecycle

    fetched = 0
    for leg in await asyncio.to_thread(_due, lifecycle.now()):
        k = _key(leg)
        _next_at[k] = lifecycle.now() + RETRY_AFTER_FAILURE
        try:
            res = await adb.fetch_flights(leg["flight_no"], leg["date_local"], use_cache=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("flight status fetch failed for %s", k)
            continue
        fetched += 1
        if not res.ok:
            continue  # keep what we knew; the retry time is already set
        flight = pick(res.flights, leg)
        if flight is None:
            continue
        now = dt.datetime.fromtimestamp(lifecycle.now(), dt.UTC)
        info = summarise(flight, now)
        _state[k] = info
        _next_at[k] = lifecycle.now() + _interval(info["state"], info["arr_utc_est"], now)
    return fetched


async def run_refresher(interval: float = 60.0) -> None:
    while True:
        try:
            await refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("flight status refresh failed")
        await asyncio.sleep(interval)
