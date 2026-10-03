"""AeroDataBox client.

Two calls matter for us:
  1. Flights by number + date  -> route, scheduled times, callsign, tail number
  2. Aircraft by registration  -> type + build date (= age)

Design rules
  * One shared httpx.AsyncClient (lazy, reusable, closed via aclose()).
  * Typed outcomes: callers can tell "no such flight" from "bad key", "quota",
    "service down" and "junk payload" (see the KIND_* constants / FlightsResult).
  * Payloads are validated BEFORE they are cached. Errors are never cached.
  * A circuit breaker stops us burning quota after a 401/403 (300s) or 429 (60s).
  * Every path segment is percent-quoted; the API key is never logged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from . import config, db

log = logging.getLogger("hypertracker.aerodatabox")

# --- outcome kinds -----------------------------------------------------------
KIND_OK = "ok"
KIND_NO_FLIGHTS = "no_flights"
KIND_BAD_KEY = "bad_key"
KIND_QUOTA = "quota"
KIND_DOWN = "upstream_down"
KIND_BAD_PAYLOAD = "bad_payload"

BREAKER_SECONDS = {KIND_BAD_KEY: 300.0, KIND_QUOTA: 60.0}
_CACHE_VERSION = "v3"
_FOREVER = 10**10  # cache_get ttl; real expiry lives in the payload wrapper
_BODY_LOG_CHARS = 200


class UpstreamError(Exception):
    """Base class for every typed upstream failure."""

    kind = KIND_DOWN

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class BadKey(UpstreamError):
    kind = KIND_BAD_KEY


class QuotaExceeded(UpstreamError):
    kind = KIND_QUOTA


class UpstreamDown(UpstreamError):
    kind = KIND_DOWN


class BadPayload(UpstreamError):
    kind = KIND_BAD_PAYLOAD


class _NoContent(Exception):
    """204/404 from the API: the question was fine, there is just nothing."""


@dataclass
class FlightsResult:
    kind: str
    flights: list = field(default_factory=list)
    status: int | None = None
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.kind == KIND_OK


# --- clocks and sleeping (module level so tests can patch them) -------------
def _now() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# --- shared client -----------------------------------------------------------
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _timeout() -> float:
    return float(getattr(config, "UPSTREAM_TIMEOUT", 8))


_transport: httpx.AsyncBaseTransport | None = None


def get_client() -> httpx.AsyncClient:
    """Lazy shared client. Recreated if the event loop it was built on is gone."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is not None and (_client.is_closed or _client_loop is not loop):
        _client = None
    if _client is None:
        _client = httpx.AsyncClient(timeout=_timeout(), transport=_transport)
        _client_loop = loop
    return _client


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Inject a transport for the shared client (tests use httpx.MockTransport)."""
    global _client, _client_loop, _transport
    _transport, _client, _client_loop = transport, None, None


async def aclose() -> None:
    global _client, _client_loop
    c, _client, _client_loop = _client, None, None
    if c is not None and not c.is_closed:
        await c.aclose()


# --- circuit breaker ---------------------------------------------------------
_breaker_until = 0.0
_breaker_kind: str | None = None


def reset_state() -> None:
    """Clear breaker state (tests, and a hook for operators)."""
    global _breaker_until, _breaker_kind, _last_call
    _breaker_until, _breaker_kind, _last_call = 0.0, None, 0.0


def breaker_state() -> tuple[str | None, float]:
    """(kind, seconds_remaining) while the breaker is open, else (None, 0)."""
    remaining = _breaker_until - _mono()
    return (_breaker_kind, remaining) if remaining > 0 else (None, 0.0)


def _trip_breaker(kind: str) -> None:
    global _breaker_until, _breaker_kind
    until = _mono() + BREAKER_SECONDS[kind]
    if until > _breaker_until:
        _breaker_until, _breaker_kind = until, kind


def _check_breaker() -> None:
    kind, remaining = breaker_state()
    if kind == KIND_BAD_KEY:
        raise BadKey(f"circuit open for another {remaining:.0f}s")
    if kind == KIND_QUOTA:
        raise QuotaExceeded(f"circuit open for another {remaining:.0f}s")


# --- request plumbing --------------------------------------------------------
def _auth_mode() -> str:
    mode = str(getattr(config, "AERODATABOX_AUTH", "rapidapi")).lower()
    if "api.market" in str(config.AERODATABOX_BASE).lower():
        return "apimarket"
    return "apimarket" if mode == "apimarket" else "rapidapi"


def _headers() -> dict:
    h = {"Accept": "application/json"}
    key = config.AERODATABOX_KEY
    if not key:
        return h
    if _auth_mode() == "apimarket":
        # API.Market gateway: ONLY this header (base URL carries the path prefix).
        h["x-api-market-key"] = key
    else:
        h["X-RapidAPI-Key"] = key
        host = getattr(config, "AERODATABOX_HOST", "")
        if host:
            h["X-RapidAPI-Host"] = host
    return h


def _note_quota_headers(path: str, r) -> None:
    """Log remaining API units when the gateway reports them (never to users)."""
    for name in ("x-ratelimit-api-units-remaining", "x-ratelimit-requests-remaining"):
        v = r.headers.get(name)
        if v is None:
            continue
        log.debug("AeroDataBox %s: %s=%s", path, name, v)
        try:
            if name.endswith("units-remaining") and float(v) < 20:
                log.warning("AeroDataBox API units remaining is low: %s", v)
        except ValueError:
            pass


def _warn(path: str, status, body: str = "") -> None:
    log.warning("AeroDataBox %s -> %s %s", path, status, (body or "")[:_BODY_LOG_CHARS])


# The RapidAPI BASIC plan allows about one request per second. A flight lookup is followed
# straight away by an aircraft lookup, so space every upstream call out.
_MIN_INTERVAL = 1.1
_pace_lock: asyncio.Lock | None = None
_pace_loop: asyncio.AbstractEventLoop | None = None
_last_call = 0.0


async def _pace() -> None:
    global _pace_lock, _pace_loop, _last_call
    loop = asyncio.get_running_loop()
    if _pace_lock is None or _pace_loop is not loop:
        _pace_lock, _pace_loop = asyncio.Lock(), loop
    async with _pace_lock:
        wait = _last_call + _MIN_INTERVAL - _mono()
        if wait > 0:
            await _sleep(wait)
        _last_call = _mono()


async def _request(path: str, params: dict | None = None):
    """GET and return parsed JSON. Raises a typed UpstreamError / _NoContent.

    One retry (with jitter) on timeout, connection error or 5xx.
    """
    if not config.AERODATABOX_KEY:
        raise BadKey("AERODATABOX_KEY is not set")
    _check_breaker()
    url = config.AERODATABOX_BASE.rstrip("/") + path
    last: UpstreamError | None = None
    for attempt in (0, 1):
        if attempt:
            await _sleep(random.uniform(0.2, 0.6))
            _check_breaker()
        try:
            await _pace()
            r = await get_client().get(url, headers=_headers(), params=params or {}, timeout=_timeout())
        except httpx.HTTPError as e:
            _warn(path, type(e).__name__)
            last = UpstreamDown(type(e).__name__)
            continue
        code = r.status_code
        _note_quota_headers(path, r)
        if code == 200:
            try:
                return r.json()
            except ValueError:
                _warn(path, code, r.text)
                raise BadPayload("not JSON", code) from None
        if code in (204, 404):
            log.info("AeroDataBox %s -> %s (nothing found)", path, code)
            raise _NoContent()
        if code in (401, 403):
            _warn(path, code, r.text)
            _trip_breaker(KIND_BAD_KEY)
            raise BadKey("rejected", code)
        if code == 429:
            _warn(path, code, r.text)
            if attempt == 0 and "per second" in r.text.lower():
                last = QuotaExceeded("per-second limit", code)
                continue  # a burst, not a spent quota: _pace() spaces the retry, no lockout
            _trip_breaker(KIND_QUOTA)
            raise QuotaExceeded("rate limited", code)
        if code >= 500:
            _warn(path, code, r.text)
            last = UpstreamDown("server error", code)
            continue
        _warn(path, code, r.text)
        raise BadPayload(f"unexpected status {code}", code)
    raise last or UpstreamDown("unavailable")


# --- payload validation ------------------------------------------------------
def validate_flights_payload(data) -> list:
    """Return the list of flight dicts or raise BadPayload.

    The flights endpoint always returns a top-level array. Every item must
    be a dict with a 'departure' or 'arrival' dict. Anything else (a
    {'message': ...} error body, null, a string) is junk and never cached.
    """
    if not isinstance(data, list):
        raise BadPayload("not a flight list")
    for item in data:
        if not isinstance(item, dict) or not (
            isinstance(item.get("departure"), dict) or isinstance(item.get("arrival"), dict)
        ):
            raise BadPayload("flight without departure/arrival")
    return data


# --- time helpers ------------------------------------------------------------
def parse_adb_dt(value) -> dt.datetime | None:
    """Parse an AeroDataBox time ('2026-06-04 12:00Z', '2026-06-04 08:00-04:00')
    into an aware UTC datetime. None for empty or unparseable input."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.astimezone(dt.UTC)


def _sched(f: dict, side: str, which: str):
    return ((f.get(side) or {}).get("scheduledTime") or {}).get(which)


def dep_local_date(f: dict) -> str:
    return str(_sched(f, "departure", "local") or "")[:10]


def _dep_utc(f: dict):
    return parse_adb_dt(_sched(f, "departure", "utc")) or parse_adb_dt(_sched(f, "departure", "local"))


def _arr_utc(f: dict):
    side = f.get("arrival") or {}
    for t in ("revisedTime", "predictedTime", "scheduledTime"):
        d = parse_adb_dt((side.get(t) or {}).get("utc"))
        if d:
            return d
    return parse_adb_dt(_sched(f, "arrival", "local"))


def _is_completed(flights: list, now: float) -> bool:
    """True when every flight in the list landed more than 3h ago."""
    if not flights:
        return False
    cutoff = dt.datetime.fromtimestamp(now - 3 * 3600, dt.UTC)
    for f in flights:
        arr = _arr_utc(f)
        if arr is None or arr > cutoff:
            return False
    return True


# --- flights by number -------------------------------------------------------
_FLIGHT_NO_RE = re.compile(r"^[A-Z0-9]{2,8}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def _cache_read(table: str, col: str, key: str):
    wrapper = await asyncio.to_thread(db.cache_get, table, col, key, _FOREVER)
    if not isinstance(wrapper, dict):
        return None
    exp = wrapper.get("_exp")
    if not isinstance(exp, (int, float)) or exp <= _now() or "data" not in wrapper:
        return None
    return wrapper


async def _cache_write(table: str, col: str, key: str, data, ttl: float, **extra) -> None:
    wrapper = {"_exp": _now() + ttl, "data": data, **extra}
    try:
        await asyncio.to_thread(db.cache_put, table, col, key, wrapper)
    except Exception:  # a cache failure must never fail the lookup
        log.exception("cache write failed for %s", key)


async def fetch_flights(flight_no: str, date_local: str, *, use_cache: bool = True) -> FlightsResult:
    """All legs flown under `flight_no` whose departure-local date is `date_local`
    (the API may also return neighbouring days; selection happens in _pick_flight).
    Never raises for upstream problems: inspect `.kind`."""
    flight_no = (flight_no or "").strip().upper()
    if not _FLIGHT_NO_RE.match(flight_no) or not _DATE_RE.match(date_local or ""):
        return FlightsResult(KIND_NO_FLIGHTS)
    key = f"{_CACHE_VERSION}|{flight_no}|{date_local}"
    if use_cache:
        w = await _cache_read("flight_cache", "cache_key", key)
        if w is not None:
            if w.get("neg"):
                return FlightsResult(KIND_NO_FLIGHTS, cached=True)
            try:
                return FlightsResult(KIND_OK, validate_flights_payload(w["data"]), cached=True)
            except BadPayload:
                pass  # poisoned entry: fall through and refetch
    path = f"/flights/Number/{quote(flight_no, safe='')}/{quote(date_local, safe='')}"
    params = {
        "withAircraftImage": "false",
        "withLocation": "false",  # airport lat/lon come back regardless
        "dateLocalRole": "Departure",  # disambiguate overnight flights
    }
    try:
        flights = validate_flights_payload(await _request(path, params))
    except _NoContent:
        flights = []
    except UpstreamError as e:
        if isinstance(e, BadPayload):
            log.warning("AeroDataBox %s: rejected payload (%s)", path, e)
        return FlightsResult(e.kind, status=e.status)
    if not flights:
        await _cache_write(
            "flight_cache", "cache_key", key, [], getattr(config, "FLIGHT_NEGATIVE_TTL", 600), neg=True
        )
        return FlightsResult(KIND_NO_FLIGHTS)
    near = getattr(config, "FLIGHT_CACHE_TTL_NEAR", 21600)
    past = getattr(config, "FLIGHT_CACHE_TTL_PAST", 2592000)
    ttl = past if _is_completed(flights, _now()) else near
    await _cache_write("flight_cache", "cache_key", key, flights, ttl)
    return FlightsResult(KIND_OK, flights)


# --- picking the right leg ---------------------------------------------------
@dataclass
class Pick:
    kind: str  # found | ambiguous | not_found
    flight: dict | None = None
    candidates: list = field(default_factory=list)  # raw flight dicts
    reason: str | None = None  # no_date_match | route_mismatch
    other_dates: list = field(default_factory=list)  # adjacent dates, for messages


def airport_codes(f: dict, side: str) -> set:
    ap = (f.get(side) or {}).get("airport") or {}
    return ({str(ap.get("icao") or "").upper(), str(ap.get("iata") or "").upper()}) - {""}


def _sort_key(f: dict):
    d = _dep_utc(f)
    return (d is None, d or dt.datetime.max.replace(tzinfo=dt.UTC), dep_local_date(f))


def _dedupe(flights: list) -> list:
    """Collapse codeshare duplicates of the same physical leg, keeping the
    operating record when there is one."""
    best: dict = {}
    order: list = []
    for f in flights:
        dep = sorted(airport_codes(f, "departure"))
        arr = sorted(airport_codes(f, "arrival"))
        d = _dep_utc(f)
        key = (tuple(dep), tuple(arr), d.isoformat() if d else dep_local_date(f))
        if key not in best:
            best[key] = f
            order.append(key)
        elif (
            best[key].get("codeshareStatus") == "IsCodeshared" and f.get("codeshareStatus") != "IsCodeshared"
        ):
            best[key] = f
    return [best[k] for k in order]


def _hint(h) -> str | None:
    return str(h or "").strip().upper() or None


def _matches(f: dict, dep_hint, arr_hint) -> bool:
    if dep_hint and dep_hint not in airport_codes(f, "departure"):
        return False
    if arr_hint and arr_hint not in airport_codes(f, "arrival"):
        return False
    return True


def _pick_flight(
    flights: list, date_local: str, dep_hint: str | None = None, arr_hint: str | None = None
) -> Pick:
    """Choose one leg, or say why we can't. Pure function.

    * Only legs whose departure-local date equals `date_local` qualify. There is
      no fallback to other dates; neighbouring dates only feed the message.
    * From/To hints (ICAO or IATA, any case) apply even to a single candidate.
    * Several candidates and no deciding hint => 'ambiguous', sorted by
      scheduled departure (UTC). We never silently pick one.
    """
    dep_hint, arr_hint = _hint(dep_hint), _hint(arr_hint)
    flights = _dedupe([f for f in (flights or []) if isinstance(f, dict)])
    same = sorted((f for f in flights if dep_local_date(f) == date_local), key=_sort_key)
    if not same:
        near = set()
        try:
            want = dt.date.fromisoformat(date_local)
            for f in flights:
                s = dep_local_date(f)
                try:
                    if s and abs((dt.date.fromisoformat(s) - want).days) <= 1:
                        near.add(s)
                except ValueError:
                    pass
        except ValueError:
            pass
        return Pick("not_found", reason="no_date_match", other_dates=sorted(near))
    matching = [f for f in same if _matches(f, dep_hint, arr_hint)]
    if not matching:
        return Pick("not_found", candidates=same, reason="route_mismatch")
    if len(matching) == 1:
        return Pick("found", flight=matching[0], candidates=matching)
    return Pick("ambiguous", candidates=matching)


async def flight_by_number(
    flight_no: str,
    date_local: str,
    dep_hint: str | None = None,
    arr_hint: str | None = None,
    *,
    use_cache: bool = True,
):
    """Fetch + pick. Returns (FlightsResult, Pick | None); Pick is None unless ok."""
    res = await fetch_flights(flight_no, date_local, use_cache=use_cache)
    if not res.ok:
        return res, None
    return res, _pick_flight(res.flights, date_local, dep_hint, arr_hint)


# --- aircraft ----------------------------------------------------------------
# [verify field names against a live response] The names below are best guesses
# from the public docs; one real /aircrafts/reg/{reg} body should be recorded as
# a fixture. When the research result arrives, edit ONLY this table.
# Field names verified against a real /aircrafts/reg/{reg} response (9V-SMG).
# There is NO typeCode field. `model` is a short code ('A359'); `typeName` is the
# full name ('Airbus A350-900'). Null fields are omitted from the JSON.
_AIRCRAFT_MAP = {
    "type": ("icaoCode", "modelCode"),  # first present wins
    "model": ("typeName", "model"),  # prefer the full name
    "age_years": ("ageYears",),  # used only when no date works
    "dates": ("rolloutDate", "firstFlightDate", "deliveryDate", "registrationDate"),
}
MAX_AGE_YEARS = 80


def _first_str(rec: dict, names) -> str | None:
    for n in names:
        v = rec.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_build_date(raw: str) -> dt.date | None:
    raw = raw.strip()
    for length, fmt in ((10, "%Y-%m-%d"), (7, "%Y-%m"), (4, "%Y")):
        try:
            return dt.datetime.strptime(raw[:length], fmt).date()
        except ValueError:
            continue
    return None


def map_aircraft(rec: dict, today: dt.date | None = None) -> dict:
    """The ONE place that maps an upstream aircraft record to our columns:
    {ac_type, ac_model, ac_age, ac_built}. Age comes from the first usable date
    (rollout, first flight, delivery, registration: a freighter conversion makes
    firstFlightDate misleading), else numeric ageYears; clamped to [0, 80]."""
    today = today or dt.datetime.fromtimestamp(_now(), dt.UTC).date()
    rec = rec if isinstance(rec, dict) else {}
    out = {
        "ac_type": _first_str(rec, _AIRCRAFT_MAP["type"]),
        "ac_model": _first_str(rec, _AIRCRAFT_MAP["model"]),
        "ac_age": None,
        "ac_built": None,
    }
    for name in _AIRCRAFT_MAP["dates"]:
        v = rec.get(name)
        built = _parse_build_date(v) if isinstance(v, str) else None
        if built:
            age = round((today - built).days / 365.25, 1)
            if 0 <= age <= MAX_AGE_YEARS:
                out["ac_age"], out["ac_built"] = age, v.strip()[:10]
                return out
    for name in _AIRCRAFT_MAP["age_years"]:
        v = rec.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= MAX_AGE_YEARS:
            out["ac_age"] = round(float(v), 1)
            break
    return out


_REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{1,9}$")


async def aircraft_by_reg(reg: str, *, use_cache: bool = True) -> dict | None:
    """Raw aircraft record (a single JSON object) for a registration, or None
    (unknown, error, junk). Best effort: enrichment must never fail a lookup, so
    errors are logged and swallowed here. 204 is negatively cached."""
    reg = (reg or "").strip().upper()
    if not _REG_RE.match(reg):
        return None
    ttl = getattr(config, "AIRCRAFT_CACHE_TTL", 60 * 60 * 24 * 90)
    if use_cache:
        w = await _cache_read("aircraft_cache", "reg", reg)
        if w is not None:
            return w["data"] if isinstance(w["data"], dict) and w["data"] else None
    path = f"/aircrafts/reg/{quote(reg, safe='')}"
    try:
        rec = await _request(path)
    except _NoContent:
        await _cache_write(
            "aircraft_cache", "reg", reg, {}, getattr(config, "FLIGHT_NEGATIVE_TTL", 600), neg=True
        )
        return None
    except UpstreamError:
        return None
    if not isinstance(rec, dict) or not rec or (len(rec) == 1 and "message" in rec):
        log.warning("AeroDataBox aircraft %s: rejected payload", reg)
        return None
    await _cache_write("aircraft_cache", "reg", reg, rec, ttl)
    return rec
