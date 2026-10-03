"""Background jobs: hourly housekeeping (purge, cache prune, orphan users), the
callsign supplier for the live-status poller, and near-term leg refresh.

Every loop logs and continues on error; nothing is swallowed silently."""

import asyncio
import datetime as dt
import logging

from . import config, db, lifecycle

log = logging.getLogger("hypertracker.jobs")

ORPHAN_USER_DAYS = 30
STALE_IDENTITY_DAYS = 90
HOUSEKEEPING_INTERVAL = 3600
CALLSIGN_REFRESH_INTERVAL = 60
REFRESH_WINDOW_HOURS = 48
REFRESH_MIN_SPACING = 6 * 3600  # don't retry the same leg more often than this


# ---------------- housekeeping ----------------
def prune_caches() -> dict:
    t = lifecycle.now()
    flight_ttl = max(
        int(config.FLIGHT_CACHE_TTL),
        int(getattr(config, "FLIGHT_CACHE_TTL_PAST", 0)),
        int(getattr(config, "FLIGHT_CACHE_TTL_NEAR", 0)),
    )
    with db.get_conn() as conn:
        f = conn.execute("DELETE FROM flight_cache WHERE fetched_at < ?", (t - flight_ttl,)).rowcount
        a = conn.execute(
            "DELETE FROM aircraft_cache WHERE fetched_at < ?", (t - int(config.AIRCRAFT_CACHE_TTL),)
        ).rowcount
    return {"flight_cache": f, "aircraft_cache": a}


def prune_orphan_users() -> int:
    """Users who have no trips and have not been seen for 30 days."""
    cutoff = lifecycle.now() - ORPHAN_USER_DAYS * 86400
    with db.get_conn() as conn:
        return conn.execute(
            "DELETE FROM users WHERE COALESCE(updated_at, 0) < ? "
            "AND discord_id NOT IN (SELECT owner_id FROM trips)",
            (cutoff,),
        ).rowcount


def prune_stale_identities() -> int:
    """Anonymous identities that own no trips and have not been used for 90 days."""
    cutoff = lifecycle.now() - STALE_IDENTITY_DAYS * 86400
    with db.get_conn() as conn:
        return conn.execute(
            "DELETE FROM identities WHERE last_seen < ? AND id NOT IN (SELECT owner_id FROM trips)",
            (cutoff,),
        ).rowcount


def housekeeping_once() -> dict:
    """One pass of all housekeeping. Each step is isolated: a failure in one is
    logged and the others still run."""
    counts: dict = {"trips": 0, "flight_cache": 0, "aircraft_cache": 0, "users": 0, "identities": 0}
    try:
        counts["trips"] = lifecycle.purge_old_trips()
    except Exception:
        log.exception("housekeeping: purging old trips failed")
    try:
        counts.update(prune_caches())
    except Exception:
        log.exception("housekeeping: pruning caches failed")
    try:
        counts["users"] = prune_orphan_users()
    except Exception:
        log.exception("housekeeping: pruning orphan users failed")
    try:
        counts["identities"] = prune_stale_identities()
    except Exception:
        log.exception("housekeeping: pruning stale identities failed")
    log.info(
        "housekeeping: purged %d trips, %d flight_cache + %d aircraft_cache rows, %d orphan users, "
        "%d stale identities",
        counts["trips"],
        counts["flight_cache"],
        counts["aircraft_cache"],
        counts["users"],
        counts["identities"],
    )
    return counts


async def run_housekeeping_loop(interval: float = HOUSEKEEPING_INTERVAL) -> None:
    """Runs once immediately, then every `interval` seconds."""
    while True:
        try:
            await asyncio.to_thread(housekeeping_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("housekeeping loop iteration failed")
        try:
            await refresh_upcoming()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("refresh_upcoming failed")
        await asyncio.sleep(interval)


# ---------------- live-status callsigns ----------------
_callsigns: frozenset[str] = frozenset()


def compute_callsigns() -> set[str]:
    """Callsigns of legs inside [departure - 20 min, arrival + 45 min]."""
    now_dt = dt.datetime.fromtimestamp(lifecycle.now(), dt.UTC)
    found: set[str] = set()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT l.callsign, l.dep_utc, l.arr_utc FROM legs l JOIN trips t ON t.id = l.trip_id "
            "WHERE l.callsign IS NOT NULL AND l.dep_utc IS NOT NULL AND l.arr_utc IS NOT NULL "
            "AND t.ends_at >= ?",
            (lifecycle.now() - 2 * 86400,),
        ).fetchall()
    for r in rows:
        dep, arr = lifecycle.parse_adb_dt(r["dep_utc"]), lifecycle.parse_adb_dt(r["arr_utc"])
        if dep and arr and dep - dt.timedelta(minutes=20) <= now_dt <= arr + dt.timedelta(minutes=45):
            found.add(r["callsign"].strip().upper())
    return found


def callsign_supplier():
    """Synchronous and cheap: returns the set last computed by the refresher."""
    return _callsigns


async def refresh_callsigns_once() -> None:
    global _callsigns
    _callsigns = frozenset(await asyncio.to_thread(compute_callsigns))


async def run_callsign_refresher(interval: float = CALLSIGN_REFRESH_INTERVAL) -> None:
    while True:
        try:
            await refresh_callsigns_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("callsign refresh failed")
        await asyncio.sleep(interval)


# ---------------- near-term refresh ----------------
_refresh_day: str | None = None
_refresh_used = 0
_refresh_last_try: dict[int, int] = {}

_REFRESH_COLS = (
    "callsign",
    "dep_icao",
    "dep_iata",
    "dep_name",
    "dep_lat",
    "dep_lon",
    "dep_local",
    "dep_utc",
    "arr_icao",
    "arr_iata",
    "arr_name",
    "arr_lat",
    "arr_lon",
    "arr_local",
    "arr_utc",
    "reg",
    "ac_type",
    "ac_model",
    "ac_age",
    "ac_built",
    "resolved",
    "unverified",
)


def reset_refresh_state() -> None:
    global _refresh_day, _refresh_used
    _refresh_day, _refresh_used = None, 0
    _refresh_last_try.clear()


def _budget_left() -> int:
    global _refresh_day, _refresh_used
    today = dt.datetime.fromtimestamp(lifecycle.now(), dt.UTC).date().isoformat()
    if _refresh_day != today:
        _refresh_day, _refresh_used = today, 0
    return max(0, int(getattr(config, "MAX_REFRESH_PER_DAY", 40)) - _refresh_used)


def _candidates() -> list[dict]:
    """Flight legs departing within 48h that still lack a registration."""
    t = lifecycle.now()
    now_dt = dt.datetime.fromtimestamp(t, dt.UTC)
    horizon = now_dt + dt.timedelta(hours=REFRESH_WINDOW_HOURS)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT l.* FROM legs l JOIN trips t ON t.id = l.trip_id "
            "WHERE l.flight_no IS NOT NULL AND l.flight_no != '' AND l.manual = 0 "
            "AND (l.reg IS NULL OR l.reg = '') AND t.ends_at >= ? ORDER BY t.ends_at",
            (t,),
        ).fetchall()
    out = []
    for r in rows:
        leg = dict(r)
        dep = lifecycle.parse_adb_dt(leg.get("dep_utc"))
        if dep is None:
            d = lifecycle._parse_date(leg.get("date_local"))
            if d is None:
                continue
            dep = dt.datetime.combine(d, dt.time(0), tzinfo=dt.UTC)
        if now_dt - dt.timedelta(hours=3) <= dep <= horizon:
            out.append(leg)
    return out


def _apply_refresh(leg: dict, changes: dict) -> None:
    cols = {k: v for k, v in changes.items() if k in _REFRESH_COLS}
    if not cols:
        return
    with db.get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE legs SET {sets} WHERE id = ?", (*cols.values(), leg["id"]))
        legs = [dict(r) for r in conn.execute("SELECT * FROM legs WHERE trip_id = ?", (leg["trip_id"],))]
        t = conn.execute("SELECT created_at FROM trips WHERE id = ?", (leg["trip_id"],)).fetchone()
        if t:
            conn.execute(
                "UPDATE trips SET ends_at = ?, updated_at = ? WHERE id = ?",
                (lifecycle.compute_ends_at(legs, t["created_at"]), lifecycle.now(), leg["trip_id"]),
            )


async def refresh_upcoming() -> int:
    """Re-resolve (bypassing the cache) legs departing within 48h that have no
    registration yet. Bounded by MAX_REFRESH_PER_DAY. Returns legs updated."""
    global _refresh_used
    from . import resolver

    refresh_leg = getattr(resolver, "refresh_leg", None)
    if refresh_leg is None:
        log.info("refresh_upcoming: resolver.refresh_leg is not available; skipping")
        return 0
    budget = _budget_left()
    if budget <= 0:
        return 0
    updated = 0
    for leg in await asyncio.to_thread(_candidates):
        if budget <= 0:
            break
        last = _refresh_last_try.get(leg["id"])
        if last is not None and lifecycle.now() - last < REFRESH_MIN_SPACING:
            continue
        _refresh_last_try[leg["id"]] = lifecycle.now()
        _refresh_used += 1
        budget -= 1
        try:
            changes = await refresh_leg(leg)
            if changes:
                await asyncio.to_thread(_apply_refresh, leg, changes)
                updated += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("refresh_upcoming: leg %s failed", leg.get("id"))
    if updated:
        log.info("refresh_upcoming: updated %d legs", updated)
    return updated
