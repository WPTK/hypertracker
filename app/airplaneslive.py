"""airplanes.live live-status poller.

A single background task (`run_poller`) probes the callsigns the board cares
about, one at a time with >= 1s between requests (the API's courtesy limit), and
keeps the answers in memory. Request handlers only call `get_state`, which never
touches the network.

State per callsign: "airborne" | "on_ground" | None (not seen, unknown, or stale).

Classification of one aircraft entry (airplanes.live v2 `ac[]` items):
  * alt_baro == "ground" (the string)          -> on_ground
  * alt_baro numeric and > 0                    -> airborne
  * alt_baro numeric and <= 0                   -> on_ground
  * alt_baro missing/other, gs (knots) numeric  -> airborne if gs > 40 else unknown
    (alt_baro is a number in feet or the string "ground"; keys are omitted when
    unknown, as seen in a real response)
  * nothing usable                              -> ignored
When several aircraft share a callsign, an airborne one wins.

Failures (429, 5xx, network, junk) never overwrite a known state and never
store "not airborne"; they are logged at WARNING at most once a minute per kind.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("hypertracker.airplaneslive")

STATE_TTL = 300.0  # seconds a state survives without a successful refresh
MIN_SPACING = 1.0  # seconds between request starts
GS_AIRBORNE_KT = 40
_WARN_EVERY = 60.0
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")

USER_AGENT = "hypertracker/1.0 (friends flight board; non-commercial)"
AIRBORNE = "airborne"
ON_GROUND = "on_ground"

# callsign -> (state | None, refreshed_at)
_states: dict[str, tuple[str | None, float]] = {}
_snapshot_at: float | None = None
_last_warn: dict[str, float] = {}


def _clock() -> float:
    return time.time()


def reset() -> None:
    """Forget everything (tests)."""
    global _snapshot_at
    _states.clear()
    _last_warn.clear()
    _snapshot_at = None


def valid_callsign(cs) -> str | None:
    cs = str(cs or "").strip().upper()
    return cs if _CALLSIGN_RE.match(cs) else None


# --- reads (no network, never block) ----------------------------------------
def get_state(callsign: str | None) -> str | None:
    cs = valid_callsign(callsign)
    if not cs:
        return None
    entry = _states.get(cs)
    if entry is None:
        return None
    state, at = entry
    if _clock() - at > STATE_TTL:
        _states.pop(cs, None)
        return None
    return state


def snapshot_updated_at() -> int | None:
    """Epoch seconds of the last successful probe, or None before the first."""
    return int(_snapshot_at) if _snapshot_at is not None else None


# --- classification ----------------------------------------------------------
def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def classify_aircraft(ac) -> str | None:
    if not isinstance(ac, dict):
        return None
    alt = ac.get("alt_baro")
    if isinstance(alt, str) and alt.strip().lower() == "ground":
        return ON_GROUND
    n = _num(alt)
    if n is not None:
        return AIRBORNE if n > 0 else ON_GROUND
    gs = _num(ac.get("gs"))
    if gs is not None:
        return AIRBORNE if gs > GS_AIRBORNE_KT else None
    return None


def classify_response(aircraft: list, callsign: str | None = None) -> str | None:
    """Collapse all aircraft sharing a callsign into one state; airborne wins.
    `flight` is space-padded to 8 chars upstream; entries naming a different
    callsign are ignored."""

    def same(a):
        f = str(a.get("flight") or "").strip().upper() if isinstance(a, dict) else ""
        return not callsign or not f or f == callsign

    states = {classify_aircraft(a) for a in aircraft if same(a)}
    if AIRBORNE in states:
        return AIRBORNE
    if ON_GROUND in states:
        return ON_GROUND
    return None


# --- polling -----------------------------------------------------------------
def _warn(kind: str, msg: str, *args) -> None:
    now = _clock()
    if now - _last_warn.get(kind, -1e9) >= _WARN_EVERY:
        _last_warn[kind] = now
        log.warning(msg, *args)


class _Failure(Exception):
    def __init__(self, kind: str, status=None):
        super().__init__(kind)
        self.kind, self.status = kind, status


async def _probe(client: httpx.AsyncClient, callsign: str) -> str | None:
    url = f"{config.AIRPLANESLIVE_BASE.rstrip('/')}/callsign/{quote(callsign, safe='')}"
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        raise _Failure("network") from e
    if r.status_code != 200:
        raise _Failure("http", r.status_code)
    try:
        data = r.json()
    except ValueError as e:
        raise _Failure("junk") from e
    ac = data.get("ac") if isinstance(data, dict) else None
    if ac is None and isinstance(data, dict):
        ac = data.get("aircraft")
    if ac is None and isinstance(data, dict):
        ac = []  # a well-formed reply with no aircraft: not visible now
    if not isinstance(ac, list):
        raise _Failure("junk")
    return classify_response(ac, callsign)


def _record(callsign: str, state: str | None) -> None:
    global _snapshot_at
    now = _clock()
    _states[callsign] = (state, now)
    _snapshot_at = now


def _prune(wanted: set[str]) -> None:
    """Bound memory: drop callsigns nobody asked for, and expired ones."""
    now = _clock()
    for cs in list(_states):
        if cs not in wanted or now - _states[cs][1] > STATE_TTL:
            del _states[cs]


async def poll_once(
    client: httpx.AsyncClient,
    callsigns: list[str],
    *,
    sleep: Callable = asyncio.sleep,
    spacing: float = MIN_SPACING,
) -> None:
    """Probe each callsign sequentially, >= `spacing` seconds between starts.
    No lock is held while sleeping."""
    last_start: float | None = None
    for cs in callsigns:
        if last_start is not None:
            wait = spacing - (_clock() - last_start)
            if wait > 0:
                await sleep(wait)
        last_start = _clock()
        try:
            _record(cs, await _probe(client, cs))
        except _Failure as f:
            # Keep whatever we knew; never store "not airborne" for a failure.
            if f.kind == "http":
                _warn(f"http{f.status}", "airplanes.live returned HTTP %s for a callsign probe", f.status)
                if f.status == 429:
                    return  # back off: abandon this cycle
            else:
                _warn(f.kind, "airplanes.live probe failed (%s)", f.kind)


async def run_poller(
    get_callsigns: Callable[[], Iterable[str]],
    interval: float = 60.0,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Callable = asyncio.sleep,
) -> None:
    """Loop forever. Cancellation-safe: CancelledError propagates after the
    client this function created (if any) is closed."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=float(getattr(config, "UPSTREAM_TIMEOUT", 8)))
    try:
        while True:
            try:
                wanted = []
                for raw in get_callsigns() or ():
                    cs = valid_callsign(raw)
                    if cs and cs not in wanted:
                        wanted.append(cs)
                _prune(set(wanted))
                await poll_once(client, wanted, sleep=sleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("airplanes.live poll cycle failed")
            await sleep(interval)
    finally:
        if own:
            await client.aclose()


def globe_url(callsign: str) -> str:
    return f"https://globe.airplanes.live/?callsign={quote(str(callsign or ''), safe='')}"


async def is_airborne(callsign: str) -> bool:
    """Back-compat shim for code not yet moved to get_state: memory only."""
    return get_state(callsign) == AIRBORNE
