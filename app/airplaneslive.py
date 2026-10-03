"""airplanes.live live-status check.

The airplanes.live globe is keyed to aircraft transmitting *right now*, so a deep
link is only meaningful while the flight is airborne. We use the free REST API
(rate-limited to 1 req/sec, non-commercial) to confirm a callsign is currently
in the air, so the board only shows the "Live" link when it actually works.

Results are cached in-process for a short window. Cache misses go through a
single async gate that enforces the 1 req/sec courtesy even when callers
(lifecycle.shape_trip) probe several legs concurrently.
"""
import asyncio
import time
import httpx
from . import config

_cache: dict[str, tuple[float, bool]] = {}
_TTL = 60  # seconds

_gate = asyncio.Lock()
_MIN_INTERVAL = 1.0  # seconds between actual API calls
_last_call = 0.0


async def is_airborne(callsign: str) -> bool:
    if not callsign:
        return False
    callsign = callsign.strip().upper()
    hit = _cache.get(callsign)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    global _last_call
    async with _gate:
        # Another waiter may have fetched this callsign while we queued.
        hit = _cache.get(callsign)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.time()

        url = f"{config.AIRPLANESLIVE_BASE.rstrip('/')}/callsign/{callsign}"
        airborne = False
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                ac = data.get("ac") or data.get("aircraft") or []
                airborne = len(ac) > 0
        except Exception:
            airborne = False

        _cache[callsign] = (time.time(), airborne)
        return airborne


def globe_url(callsign: str) -> str:
    return f"https://globe.airplanes.live/?callsign={callsign}"
