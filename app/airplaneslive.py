"""airplanes.live live-status check.

The airplanes.live globe is keyed to aircraft transmitting *right now*, so a deep
link is only meaningful while the flight is airborne. We use the free REST API
(rate-limited to 1 req/sec, non-commercial) to confirm a callsign is currently
in the air, so the board only shows the "Live" link when it actually works.

Results are cached in-process for a short window to respect the rate limit.
"""
import time
import httpx
from . import config

_cache: dict[str, tuple[float, bool]] = {}
_TTL = 60  # seconds


async def is_airborne(callsign: str) -> bool:
    if not callsign:
        return False
    callsign = callsign.strip().upper()
    now = time.time()
    hit = _cache.get(callsign)
    if hit and now - hit[0] < _TTL:
        return hit[1]

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

    _cache[callsign] = (now, airborne)
    return airborne


def globe_url(callsign: str) -> str:
    return f"https://globe.airplanes.live/?callsign={callsign}"
