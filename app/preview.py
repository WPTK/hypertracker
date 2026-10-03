"""POST /api/legs/preview: resolve rows for the trip form without writing anything.

Mounted by the backend core with `app.include_router(preview.router)`.
Its own loose per-IP limiter keeps a chatty form from tripping the write limit.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import time
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, db, resolver
from .netutil import client_ip
from .validation import is_valid_flight_no, normalize_airport_code, normalize_flight_no

router = APIRouter()

_hits: dict[str, collections.deque] = {}
_MAX_TRACKED_IPS = 5000


def _now() -> float:
    return time.time()


def reset_rate_limit() -> None:
    _hits.clear()


def _rate_limited(ip: str) -> int | None:
    """Record a hit; return seconds to wait if over the limit, else None."""
    limit = int(getattr(config, "PREVIEW_RATE_LIMIT", 60))
    window = float(getattr(config, "PREVIEW_RATE_WINDOW", 600))
    now = _now()
    if len(_hits) > _MAX_TRACKED_IPS:
        for k in [k for k, q in _hits.items() if not q or now - q[-1] > window]:
            del _hits[k]
    q = _hits.setdefault(ip, collections.deque())
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return max(1, int(window - (now - q[0])) + 1)
    q.append(now)
    return None


class PreviewRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["flight", "manual"]
    flight_no: str | None = Field(None, max_length=64)
    date: str | None = Field(None, max_length=64)
    from_: str | None = Field(None, alias="from", max_length=64)
    to: str | None = Field(None, max_length=64)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rows: list[PreviewRow]


def _max_rows() -> int:
    return int(getattr(config, "MAX_LEGS_PER_TRIP", 4)) * 2


def _error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status, headers=headers)


def _entry(index: int, status: str, message: str, leg=None, candidates=None) -> dict:
    return {"index": index, "status": status, "message": message, "leg": leg, "candidates": candidates or []}


def _fa_url(callsign, flight_no) -> str | None:
    ident = callsign or flight_no
    return f"https://flightaware.com/live/flight/{quote(str(ident), safe='')}" if ident else None


def to_api_leg(row: dict) -> dict:
    """DB-column leg (as the resolver returns it) -> contract Leg shape."""

    def city(code):
        ap = db.find_airport(code) if code else None
        return (ap or {}).get("municipality")

    return {
        "direction": row.get("direction"),
        "seq": row.get("seq"),
        "date_local": row.get("date_local"),
        "flight_no": row.get("flight_no"),
        "callsign": row.get("callsign"),
        "from": row.get("dep_icao"),
        "from_iata": row.get("dep_iata"),
        "from_name": row.get("dep_name"),
        "from_city": city(row.get("dep_icao")),
        "from_lat": row.get("dep_lat"),
        "from_lon": row.get("dep_lon"),
        "to": row.get("arr_icao"),
        "to_iata": row.get("arr_iata"),
        "to_name": row.get("arr_name"),
        "to_city": city(row.get("arr_icao")),
        "to_lat": row.get("arr_lat"),
        "to_lon": row.get("arr_lon"),
        "dep_local": row.get("dep_local"),
        "arr_local": row.get("arr_local"),
        "dep_utc": row.get("dep_utc"),
        "arr_utc": row.get("arr_utc"),
        "reg": row.get("reg"),
        "ac_type": row.get("ac_type"),
        "ac_model": row.get("ac_model"),
        "ac_age": row.get("ac_age"),
        "ac_built": row.get("ac_built"),
        "resolved": bool(row.get("resolved")),
        "manual": bool(row.get("manual")),
        "unverified": bool(row.get("unverified")),
        "live_state": None,
        "fa_url": _fa_url(row.get("callsign"), row.get("flight_no")),
    }


def _prepare(index: int, r: PreviewRow, today: dt.date):
    """-> (resolver_row | None, early_entry | None)."""
    date_iso, bad, msg = resolver.check_date(r.date, today)
    if bad:
        return None, _entry(index, bad, msg)
    dep_raw, arr_raw = (r.from_ or "").strip(), (r.to or "").strip()
    dep, arr = normalize_airport_code(dep_raw), normalize_airport_code(arr_raw)
    for raw, code in ((dep_raw, dep), (arr_raw, arr)):
        if raw and not code:
            return None, _entry(
                index,
                "invalid",
                f'"{raw[:12]}" doesn\'t look like an airport code. Use a 3 or 4 letter code.',
            )
    if r.kind == "flight":
        fn = normalize_flight_no(r.flight_no)
        if not fn:
            return None, _entry(index, "invalid", "Add a flight number, like DL1200.")
        if not is_valid_flight_no(fn):
            return None, _entry(
                index, "invalid", f"{fn[:12]} doesn't look like a flight number. Try something like DL1200."
            )
    else:
        fn = ""
        if not (dep and arr):
            return None, _entry(index, "invalid", "Add both airports.")
        if dep == arr:
            return None, _entry(index, "invalid", "The two airports can't be the same.")
    return {"direction": "out", "seq": index, "flight_no": fn, "date": date_iso, "from": dep, "to": arr}, None


@router.post("/api/legs/preview")
async def preview_legs(request: Request):
    wait = _rate_limited(client_ip(request))
    if wait is not None:
        return _error(
            429,
            "You're checking flights quickly. Give it a moment and try again.",
            {"Retry-After": str(wait)},
        )
    try:
        body = await request.json()
    except Exception:
        return _error(400, "I couldn't read that request.")
    try:
        req = PreviewRequest.model_validate(body)
    except ValidationError:
        return _error(400, "That request isn't in the shape I expected.")
    limit = _max_rows()
    if len(req.rows) > limit:
        return _error(400, f"That's too many legs at once. The limit is {limit}.")

    today = dt.datetime.now(dt.UTC).date()
    entries: list = [None] * len(req.rows)
    todo: list = []
    for i, r in enumerate(req.rows):
        row, early = _prepare(i, r, today)
        if early:
            entries[i] = early
        else:
            todo.append((i, row))
    if todo:
        results = await resolver.resolve_rows([row for _, row in todo])
        for (i, _), res in zip(todo, results, strict=False):
            ok = res["status"] in ("ok", "manual_ok")
            leg = await asyncio.to_thread(to_api_leg, res) if ok else None
            entries[i] = _entry(i, res["status"], res["message"], leg, res.get("candidates"))
    return {"results": entries}
