"""Hyperfixed Flight Tracker: web app (pages + JSON API + optional Discord OAuth)."""

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (
    aerodatabox,
    airplaneslive,
    auth,
    config,
    db,
    jobs,
    lifecycle,
    netutil,
    preview,
    resolver,
    schemas,
)
from .validation import is_valid_flight_no, normalize_airport_code, normalize_flight_no, validate_leg_date

log = logging.getLogger("hypertracker")

SESSION_MAX_AGE = 14 * 24 * 3600
MAX_TRIP_ID = 2**31 - 1
OK_STATUSES = ("ok", "manual_ok")
# Statuses a user may knowingly keep (stored with unverified = 1).
UNVERIFIED_OK = ("not_found", "upstream_unavailable", "quota")


def _supervised(name: str, factory, restart_delay: float = 30.0):
    """Run a background coroutine forever; log and restart if it crashes."""

    async def runner():
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background task %s crashed; restarting in %ss", name, restart_delay)
                await asyncio.sleep(restart_delay)

    return asyncio.create_task(runner(), name=name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.validate()  # refuse to start on unsafe configuration
    await asyncio.to_thread(db.init_db)
    tasks = [
        _supervised("housekeeping", jobs.run_housekeeping_loop),
        _supervised("callsigns", jobs.run_callsign_refresher),
    ]
    poller = getattr(airplaneslive, "run_poller", None)
    if poller is None:
        log.warning("airplaneslive.run_poller is not available; live status disabled")
    else:
        tasks.append(_supervised("live-poller", lambda: poller(jobs.callsign_supplier)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await aerodatabox.aclose()


app = FastAPI(title="Hyperfixed Flight Tracker", root_path=config.ROOT_PATH, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="lax",
    max_age=SESSION_MAX_AGE,
    # Mark the session cookie Secure whenever the public URL is https.
    https_only=config.BASE_URL.startswith("https"),
)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

app.include_router(preview.router)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(), usb=()")
    if config.BASE_URL.startswith("https"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    """FastAPI's own parameter validation (path/query) becomes a 400 in the
    contract's error shape, never a 422."""
    errs = exc.errors()
    loc = ".".join(str(p) for p in errs[0].get("loc", ()) if p != "path") if errs else ""
    return JSONResponse(
        {"detail": f"invalid value for '{loc}'" if loc else "invalid request"}, status_code=400
    )


templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

MANAGE_NAME_MAX = 40
GLOBAL_WRITE_MULTIPLIER = 10


# ---------------- rate limiting ----------------
def _check_write_rate(request: Request) -> None:
    """Per-IP sliding window plus a global cap (10x) so address rotation cannot
    spam. Admins are exempt, so cleaning up after a spammer never trips it."""
    if auth.is_admin(auth.current_user(request)):
        return
    netutil.rate_limit(
        request,
        "write",
        config.WRITE_RATE_LIMIT,
        config.WRITE_RATE_WINDOW,
        global_limit=config.WRITE_RATE_LIMIT * GLOBAL_WRITE_MULTIPLIER,
        message="too many changes from this address, try again in a few minutes",
    )


# ---------------- manage tokens / identity ----------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_owner_id(owner_id: str) -> str:
    """A stable, keyed hash of an owner id for the public API. The same owner
    always maps to the same value (so the board can group a person's trips and
    a logged-in browser can recognise its own) but it's keyed with a key derived
    from SECRET_KEY that is distinct from the session signing key, so the value
    is meaningless off this server and a real Discord ID is never exposed."""
    return hmac.new(
        config.derived_key("public-owner-id").encode("utf-8"), owner_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def _new_manual_uid() -> str:
    # Namespaced so it can never collide with a numeric Discord ID.
    return "m_" + secrets.token_urlsafe(9)


def _resolve_anon_identity(uid: str | None, proof: str | None) -> tuple[str, str | None]:
    """Pick the owner id for an anonymous create. Returns (uid, new_secret);
    new_secret is set only when an identity was created or claimed now.

    Owner ids are visible in the public trips API (the board needs them for
    grouping), so reusing one needs proof: the identity secret. For identities
    that predate the identities table, a manage token of a trip that uid still
    owns is accepted once and the identity is claimed."""
    now = lifecycle.now()
    if uid and uid.startswith("m_") and proof:
        ph = _hash_token(proof)
        with db.get_conn() as conn:
            row = conn.execute("SELECT secret_hash FROM identities WHERE id = ?", (uid,)).fetchone()
            if row:
                if hmac.compare_digest(ph, row["secret_hash"]):
                    conn.execute("UPDATE identities SET last_seen = ? WHERE id = ?", (now, uid))
                    return uid, None
            else:
                tokens = conn.execute(
                    "SELECT manage_token FROM trips WHERE owner_id = ? AND manage_token IS NOT NULL",
                    (uid,),
                ).fetchall()
                if any(hmac.compare_digest(ph, r["manage_token"]) for r in tokens):
                    secret = secrets.token_urlsafe(24)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO identities (id, secret_hash, created_at, last_seen) "
                        "VALUES (?,?,?,?)",
                        (uid, _hash_token(secret), now, now),
                    )
                    if cur.rowcount:
                        return uid, secret
    new_uid = _new_manual_uid()
    secret = secrets.token_urlsafe(24)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO identities (id, secret_hash, created_at, last_seen) VALUES (?,?,?,?)",
            (new_uid, _hash_token(secret), now, now),
        )
    return new_uid, secret


def _authorize_manage(request: Request, trip_row, manage_token: str | None) -> None:
    """Allow a trip edit/delete if the caller is an admin, the logged-in owner,
    or presents the per-trip manage token the browser was given at creation."""
    user = auth.current_user(request)
    if user and auth.is_admin(user):
        return
    if user and trip_row["owner_id"] == user["id"]:
        return
    stored = trip_row["manage_token"] if "manage_token" in trip_row.keys() else None
    if manage_token and stored and hmac.compare_digest(_hash_token(manage_token), stored):
        return
    raise HTTPException(403, "not allowed to manage this trip")


def _base(request: Request) -> str:
    return request.scope.get("root_path", "") or ""


def _is_configured() -> bool:
    fn = getattr(auth, "is_configured", None)
    return bool(fn()) if fn else bool(config.DISCORD_CLIENT_ID)


# ---------------- pages ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = auth.current_user(request)
    ctx = {"user": user, "base_path": _base(request)}
    # The board is gated only when OPEN_BOARD is off and nobody is logged in.
    ctx["gated"] = not user and not config.OPEN_BOARD
    ctx["is_admin"] = auth.is_admin(user)
    ctx["discord_enabled"] = _is_configured() or config.DEV_MODE
    return templates.TemplateResponse(request, "board.html", ctx)


# ---------------- auth ----------------
@app.get("/login")
def login(request: Request):
    if not _is_configured():
        return RedirectResponse("./?auth=unavailable")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(auth.login_url(state))


@app.get("/auth/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    expected = request.session.pop("oauth_state", None)
    if error or not code:
        return RedirectResponse("./?auth=denied")
    # The state must match what /login stored; otherwise this callback wasn't
    # initiated here (login-CSRF) and must not complete.
    if not expected or not state or not hmac.compare_digest(state, expected):
        return RedirectResponse("./?auth=error")
    try:
        info = await auth.exchange_code(code)
        me = info["me"]
        user = {"id": str(me["id"]), "name": auth.display_name(me), "discord_id": str(me["id"])}
    except Exception as exc:  # DiscordError, or a malformed reply
        log.warning("discord login failed: %r", exc)
        return RedirectResponse("./?auth=error")
    if not auth.in_required_guild(info.get("guilds") or []):
        return RedirectResponse("./?auth=not_member")
    await asyncio.to_thread(db.upsert_user, user["id"], user["name"])
    request.session.clear()
    request.session["user"] = user
    return RedirectResponse("./")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("./", status_code=303)


# ---------------- API: read ----------------
def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for part in header.split(","):
        part = part.strip()
        if part == "*" or part.removeprefix("W/") == etag:
            return True
    return False


def _live_updated_at(fallback: int) -> int:
    v = airplaneslive.snapshot_updated_at()
    return int(v) if v else fallback


@app.get("/api/trips")
def get_trips(request: Request):
    user = auth.current_user(request)
    if not config.OPEN_BOARD and not user:
        raise HTTPException(401, "login required")
    trips = lifecycle.active_trips_sync()
    # Anonymise owner ids before they leave the server. `me` is hashed with the
    # same function so the frontend's "is this my trip?" comparison still holds.
    for t in trips:
        t["owner_id"] = public_owner_id(t["owner_id"])
    body = {
        "trips": trips,
        "me": public_owner_id(user["id"]) if user else None,
        "is_admin": auth.is_admin(user),
    }
    # The ETag covers trips and live states; the clock fields change every
    # second and are deliberately left out of it.
    etag = (
        '"'
        + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[
            :32
        ]
        + '"'
    )
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    server_time = lifecycle.now()
    body["server_time"] = server_time
    body["live_updated_at"] = _live_updated_at(server_time)
    return JSONResponse(body, headers=headers)


@app.get("/api/airports/search")
def airport_search(request: Request, q: str = Query("")):
    netutil.rate_limit(request, "search", config.SEARCH_RATE_LIMIT, config.SEARCH_RATE_WINDOW)
    return {"results": db.search_airports(q[: db.SEARCH_Q_MAX])}


# ---------------- API: write ----------------
def _row_error(direction: str, index: int, status: str, message: str) -> dict:
    return {"direction": direction, "index": index, "status": status, "message": message}


def _fail(message: str, rows: list[dict] | None = None, code: int = 400):
    detail = {"message": message, "rows": rows} if rows else message
    raise HTTPException(code, detail)


def _date_error_status(raw: str | None) -> str:
    try:
        dt.date.fromisoformat((raw or "").strip()[:10])
        return "out_of_window"
    except ValueError:
        return "invalid"


def _blank_leg(direction: str, seq: int, date_local: str | None, flight_no: str | None) -> dict:
    leg = {c: None for c in _LEG_COLS}
    leg.update(
        direction=direction,
        seq=seq,
        date_local=date_local,
        flight_no=flight_no,
        resolved=0,
        manual=0,
        unverified=0,
    )
    return leg


def _prepare_rows(trip: schemas.TripIn):
    """Validate every row and build manual legs. Returns
    (flight_inputs, manual_slots, errors): `flight_inputs` are resolver rows,
    `slots` is the ordered list of (kind, payload) so results can be merged
    back in submitted order. Runs in a worker thread (looks up airports)."""
    today = dt.datetime.fromtimestamp(lifecycle.now(), dt.UTC).date()
    past = int(getattr(config, "FLIGHT_WINDOW_PAST_DAYS", 2))
    future = int(getattr(config, "FLIGHT_WINDOW_FUTURE_DAYS", 330))
    flight_inputs: list[dict] = []
    slots: list[dict] = []
    errors: list[dict] = []
    for direction in ("out", "ret"):
        rows = getattr(trip, direction)
        if len(rows) > config.MAX_LEGS_PER_TRIP:
            _fail(f"too many legs (max {config.MAX_LEGS_PER_TRIP} per direction)")
        seq = 0
        for idx, row in enumerate(rows):
            raw_fn, raw_from, raw_to = row.flight_no or "", row.from_ or "", row.to or ""
            if not (raw_fn.strip() or raw_from or raw_to):
                continue  # blank row from the form; ignore
            date_iso, derr = validate_leg_date(row.date, today, past, future)
            if derr:
                errors.append(_row_error(direction, idx, _date_error_status(row.date), derr))
                continue
            frm = normalize_airport_code(raw_from) if raw_from else None
            to = normalize_airport_code(raw_to) if raw_to else None
            if (raw_from and not frm) or (raw_to and not to):
                errors.append(
                    _row_error(
                        direction, idx, "invalid", "Airport codes are 3 or 4 letters, like DEN or KDEN."
                    )
                )
                continue
            fn = normalize_flight_no(raw_fn)
            if fn:
                if not is_valid_flight_no(fn):
                    errors.append(
                        _row_error(
                            direction,
                            idx,
                            "invalid",
                            "That doesn't look like a flight number. Try something like DL1200.",
                        )
                    )
                    continue
                flight_inputs.append(
                    {
                        "direction": direction,
                        "seq": seq,
                        "flight_no": fn,
                        "date": date_iso,
                        "from": frm,
                        "to": to,
                    }
                )
                slots.append(
                    {
                        "kind": "flight",
                        "direction": direction,
                        "index": idx,
                        "seq": seq,
                        "n": len(flight_inputs) - 1,
                        "flight_no": fn,
                        "date": date_iso,
                    }
                )
            else:
                if not (frm and to):
                    errors.append(
                        _row_error(direction, idx, "invalid", "Add a flight number, or both airports.")
                    )
                    continue
                a, b = db.find_airport(frm), db.find_airport(to)
                missing = [c for c, ap in ((frm, a), (to, b)) if not ap]
                if missing:
                    errors.append(
                        _row_error(
                            direction,
                            idx,
                            "airport_unknown",
                            f"Unknown airport {missing[0]}. Check the code.",
                        )
                    )
                    continue
                if a["ident"] == b["ident"]:
                    errors.append(
                        _row_error(
                            direction, idx, "invalid", "The departure and arrival airports are the same."
                        )
                    )
                    continue
                leg = _blank_leg(direction, seq, date_iso, None)
                leg.update(
                    manual=1,
                    dep_icao=a["ident"],
                    dep_iata=a.get("iata"),
                    dep_name=a.get("name"),
                    dep_lat=a.get("lat"),
                    dep_lon=a.get("lon"),
                    arr_icao=b["ident"],
                    arr_iata=b.get("iata"),
                    arr_name=b.get("name"),
                    arr_lat=b.get("lat"),
                    arr_lon=b.get("lon"),
                )
                slots.append({"kind": "manual", "direction": direction, "index": idx, "seq": seq, "leg": leg})
            seq += 1
    return flight_inputs, slots, errors


async def _build_legs(trip: schemas.TripIn, old_legs: list[dict] | None = None) -> list[dict]:
    """Validate rows, resolve flight rows, enforce accept_unverified, and return
    DB-ready legs. Raises HTTPException(400) with per-row errors."""
    flight_inputs, slots, errors = await asyncio.to_thread(_prepare_rows, trip)
    if errors:
        _fail("Some rows need fixing before this trip can be saved.", errors)
    if not slots:
        _fail("Add at least one flight.")

    results: list[dict] = []
    if flight_inputs:
        try:
            results = await resolver.resolve_rows(flight_inputs, deadline=float(config.RESOLVE_DEADLINE))
            if not isinstance(results, list) or len(results) != len(flight_inputs):
                raise ValueError("resolver returned a mismatched result")
        except Exception:
            log.exception("resolve_rows failed")
            results = [
                {
                    "status": "upstream_unavailable",
                    "message": "The flight data service is not responding. Try again in a minute.",
                }
            ] * len(flight_inputs)

    legs: list[dict] = []
    for slot in slots:
        if slot["kind"] == "manual":
            legs.append(slot["leg"])
            continue
        res = dict(results[slot["n"]])
        res["_slot"] = slot
        legs.append(res)

    if old_legs:
        _preserve_resolved(legs, old_legs)

    row_errors: list[dict] = []
    final: list[dict] = []
    for leg, slot in zip(legs, slots, strict=False):
        if slot["kind"] == "manual":
            final.append(leg)
            continue
        status = leg.get("status") or "not_found"
        if status in OK_STATUSES:
            unverified = False
        elif status in UNVERIFIED_OK and trip.accept_unverified:
            unverified = True
        else:
            row_errors.append(
                _row_error(
                    slot["direction"],
                    slot["index"],
                    status,
                    leg.get("message") or "Could not confirm that flight.",
                )
            )
            continue
        out = _blank_leg(slot["direction"], slot["seq"], slot["date"], slot["flight_no"])
        for c in _LEG_COLS:
            if c in leg and c not in ("trip_id", "direction", "seq", "date_local", "flight_no"):
                out[c] = leg[c]
        out["unverified"] = 1 if unverified else 0
        if unverified:
            out["resolved"] = 0
        else:
            out["resolved"] = 1 if (leg.get("resolved") or status == "ok") else 0
        out["manual"] = 1 if out.get("manual") else 0
        final.append(out)
    if row_errors:
        _fail("Some flights need attention before this trip can be saved.", row_errors)
    return final


_LEG_COLS = (
    "trip_id",
    "direction",
    "seq",
    "date_local",
    "flight_no",
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
    "manual",
    "unverified",
)

# Leg data carried over from a previous version of the leg when a re-resolve
# fails: everything except identity/order (direction, seq, flight_no,
# date_local).
_PRESERVE_FIELDS = (
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
    "manual",
)

_SOFT_FAIL = UNVERIFIED_OK


def _preserve_resolved(new_legs: list[dict], old_legs: list[dict]) -> None:
    """On edit, if re-resolving a leg failed (API outage, expired cache) but its
    flight number and date are unchanged, keep the previously resolved data
    instead of silently downgrading the leg. Applies to manual-route legs that
    carry a flight number too. Only failing legs are touched."""
    old = {}
    for l in old_legs:
        if l.get("resolved") and l.get("flight_no"):
            old[(l["flight_no"], l.get("date_local"))] = l
    for leg in new_legs:
        slot = leg.get("_slot")
        if not slot or slot["kind"] != "flight":
            continue
        if leg.get("status") in OK_STATUSES or leg.get("status") not in _SOFT_FAIL:
            continue
        prev = old.get((slot["flight_no"], slot["date"]))
        if prev:
            for f in _PRESERVE_FIELDS:
                leg[f] = prev.get(f)
            leg["status"] = "ok"
            leg["message"] = "Kept the details saved earlier."


def _insert_legs(conn, trip_id: int, legs: list[dict]) -> None:
    for leg in legs:
        leg["trip_id"] = trip_id
        conn.execute(
            f"INSERT INTO legs ({','.join(_LEG_COLS)}) VALUES ({','.join('?' * len(_LEG_COLS))})",
            tuple(leg.get(c) for c in _LEG_COLS),
        )


def _insert_trip(
    owner_id: str,
    owner_name: str,
    tz: str | None,
    legs: list[dict],
    manage_token_hash: str | None,
    upsert_tz_user: bool,
) -> int:
    created = lifecycle.now()
    ends_at = lifecycle.compute_ends_at(legs, created)
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trips (owner_id, owner_name, submitter_tz, created_at, manage_token, ends_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (owner_id, owner_name, tz, created, manage_token_hash, ends_at, created),
        )
        trip_id = cur.lastrowid
        _insert_legs(conn, trip_id, legs)
    if upsert_tz_user and tz:
        db.upsert_user(owner_id, owner_name, tz)
    return trip_id


def _read_trip(trip_id: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        old_legs = (
            [dict(l) for l in conn.execute("SELECT * FROM legs WHERE trip_id = ?", (trip_id,)).fetchall()]
            if row
            else []
        )
    return row, old_legs


def _write_edit(trip_id: int, expected_updated_at: int, tz: str | None, legs: list[dict]) -> None:
    """Delete + insert in one transaction. Re-checks the row inside the write
    lock: 404 if it vanished, 409 if someone else changed it meanwhile."""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        if row["updated_at"] != expected_updated_at:
            raise HTTPException(409, "this trip was changed while you were editing; reload and try again")
        ends_at = lifecycle.compute_ends_at(legs, row["created_at"])
        updated = max(lifecycle.now(), row["updated_at"] + 1)
        conn.execute("DELETE FROM legs WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "UPDATE trips SET submitter_tz = ?, ends_at = ?, updated_at = ? WHERE id = ?",
            (tz or row["submitter_tz"], ends_at, updated, trip_id),
        )
        _insert_legs(conn, trip_id, legs)


@app.post("/api/trips")
async def create_trip(request: Request):
    _check_write_rate(request)
    user = auth.current_user(request)
    trip = await schemas.parse_body(request, schemas.TripIn, max_bytes=config.MAX_BODY_BYTES)

    # Identity: a logged-in Discord user, or a name-only manual submitter.
    manage_token = None
    manage_hash = None
    if user:
        owner_id, owner_name = user["id"], user["name"]
    else:
        if not config.OPEN_BOARD:
            raise HTTPException(401, "login required")
        owner_name = trip.name or ""
        if not owner_name:
            raise HTTPException(400, "a name is required to post without logging in")
        # Anonymous posters get a secret token (stored hashed) that lets the
        # browser edit/remove this trip later without an account.
        manage_token = secrets.token_urlsafe(24)
        manage_hash = _hash_token(manage_token)

    legs = await _build_legs(trip)
    identity_secret = None
    if not user:
        owner_id, identity_secret = await asyncio.to_thread(_resolve_anon_identity, trip.uid, trip.proof)
    trip_id = await asyncio.to_thread(
        _insert_trip, owner_id, owner_name, trip.tz, legs, manage_hash, bool(user)
    )
    resp = {"ok": True, "trip_id": trip_id}
    if manage_token:
        # Returned exactly once; the browser stores it (and the uid) locally.
        resp.update(manage_token=manage_token, uid=owner_id)
        if identity_secret:
            # Also returned exactly once, and only when an identity was created or claimed.
            resp["identity_secret"] = identity_secret
    return resp


@app.put("/api/trips/{trip_id}")
async def edit_trip(
    request: Request,
    trip_id: int = Path(..., ge=1, le=MAX_TRIP_ID),
    x_manage_token: str | None = Header(default=None),
):
    _check_write_rate(request)
    row, old_legs = await asyncio.to_thread(_read_trip, trip_id)
    if not row:
        raise HTTPException(404, "not found")
    _authorize_manage(request, row, x_manage_token)

    trip = await schemas.parse_body(request, schemas.TripIn, max_bytes=config.MAX_BODY_BYTES)
    legs = await _build_legs(trip, old_legs)
    await asyncio.to_thread(_write_edit, trip_id, row["updated_at"], trip.tz, legs)
    return {"ok": True, "trip_id": trip_id}


def _delete_trip(request: Request, trip_id: int, token: str | None) -> None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        _authorize_manage(request, row, token)
        conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))


@app.delete("/api/trips/{trip_id}")
def delete_trip(
    request: Request,
    trip_id: int = Path(..., ge=1, le=MAX_TRIP_ID),
    x_manage_token: str | None = Header(default=None),
):
    _check_write_rate(request)
    _delete_trip(request, trip_id, x_manage_token)
    return {"ok": True}
