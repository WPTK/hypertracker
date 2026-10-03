"""Hyperfixed Flight Tracker — web app (pages + JSON API + optional Discord OAuth)."""
import asyncio
import hashlib
import logging
import hmac
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, auth, resolver, lifecycle


async def _purge_loop():
    """Daily cleanup of trips that left the board long ago (TRIP_PURGE_DAYS)."""
    while True:
        try:
            lifecycle.purge_old_trips()
        except Exception:
            pass  # housekeeping must never take the app down
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Guardrail: shipping DEV_MODE to a public (https) deployment makes every
    # visitor an admin. Warn loudly rather than fail silently.
    if config.DEV_MODE and config.BASE_URL.startswith("https"):
        logging.getLogger("uvicorn.error").warning(
            "DEV_MODE is ON with an https BASE_URL (%s): every visitor is treated "
            "as an admin. Set DEV_MODE=false for production.", config.BASE_URL)
    db.init_db()
    purge_task = asyncio.create_task(_purge_loop())
    yield
    purge_task.cancel()


app = FastAPI(title="Hyperfixed Flight Tracker", root_path=config.ROOT_PATH, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="lax",
    # Mark the session cookie Secure whenever the public URL is https.
    https_only=config.BASE_URL.startswith("https"),
)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Cheap, non-breaking response hardening: stop MIME sniffing, refuse to be
    framed (clickjacking), and limit referrer leakage to outbound links."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

MANAGE_NAME_MAX = 40


# ---------------- write rate limiting ----------------
_write_log: dict[str, deque] = {}


def _client_ip(request: Request) -> str:
    """Real client IP. Behind the Cloudflare Tunnel / nginx the TCP peer is
    always localhost, so trust the forwarding headers. uvicorn binds to
    127.0.0.1 in deployment, so these can't be spoofed from outside."""
    h = request.headers
    return (h.get("cf-connecting-ip")
            or (h.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def _check_write_rate(request: Request) -> None:
    """Sliding-window per-IP limit on writes. Admins are exempt, so cleaning up
    after a spammer never trips the limiter itself."""
    if auth.is_admin(auth.current_user(request)):
        return
    ip = _client_ip(request)
    now = time.time()
    q = _write_log.setdefault(ip, deque())
    while q and now - q[0] > config.WRITE_RATE_WINDOW:
        q.popleft()
    if len(q) >= config.WRITE_RATE_LIMIT:
        raise HTTPException(429, "too many changes from this address — try again in a few minutes")
    q.append(now)
    # Keep the table from growing without bound under address churn.
    if len(_write_log) > 512:
        stale = [k for k, v in _write_log.items() if not v or now - v[-1] > config.WRITE_RATE_WINDOW]
        for k in stale:
            _write_log.pop(k, None)


# ---------------- manage tokens / identity ----------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_owner_id(owner_id: str) -> str:
    """A stable, salted hash of an owner id for the public API. The same owner
    always maps to the same value (so the board can group a person's trips and
    a logged-in browser can still recognise its own), but it's keyed to this
    instance's SECRET_KEY, so the value is meaningless off this server: a real
    Discord ID is never exposed, and trips here can't be correlated by ID to
    the same person elsewhere. Raw owner ids are kept internally (database,
    session, manage/proof checks); only what /api/trips emits is anonymised."""
    return hmac.new(config.SECRET_KEY.encode("utf-8"),
                    owner_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _new_manual_uid() -> str:
    # Namespaced so it can never collide with a numeric Discord ID.
    return "m_" + secrets.token_urlsafe(9)


def _verify_uid_proof(uid: str, proof: str | None) -> bool:
    """A browser may reuse its m_ uid only by presenting a manage token for a
    trip that uid already owns. Owner ids are visible in the public trips API
    (the board needs them for grouping), so without this check anyone could
    post trips under someone else's name group."""
    if not proof:
        return False
    ph = _hash_token(proof)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT manage_token FROM trips WHERE owner_id = ? AND manage_token IS NOT NULL",
            (uid,),
        ).fetchall()
    return any(hmac.compare_digest(ph, r["manage_token"]) for r in rows)


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


def _home(request: Request, suffix: str = "/") -> str:
    return _base(request) + suffix


# ---------------- pages ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = auth.current_user(request)
    ctx = {"user": user, "base_path": _base(request)}
    # The board is gated only when OPEN_BOARD is off and nobody is logged in.
    ctx["gated"] = not user and not config.OPEN_BOARD
    ctx["is_admin"] = auth.is_admin(user)
    ctx["discord_enabled"] = bool(config.DISCORD_CLIENT_ID) or config.DEV_MODE
    return templates.TemplateResponse(request, "board.html", ctx)


# ---------------- auth ----------------
@app.get("/login")
def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(auth.login_url(state))


@app.get("/auth/callback")
async def callback(request: Request, code: str | None = None,
                   state: str | None = None, error: str | None = None):
    expected = request.session.pop("oauth_state", None)
    if error or not code:
        return RedirectResponse(_home(request, "/?auth=denied"))
    # The state must match what /login stored — otherwise this callback wasn't
    # initiated here (login-CSRF) and must not complete.
    if not expected or not state or not hmac.compare_digest(state, expected):
        return RedirectResponse(_home(request, "/?auth=error"))
    try:
        info = await auth.exchange_code(code)
    except Exception:
        return RedirectResponse(_home(request, "/?auth=error"))
    if not auth.in_required_guild(info["guilds"]):
        return RedirectResponse(_home(request, "/?auth=not_member"))
    me = info["me"]
    user = {"id": str(me["id"]), "name": auth.display_name(me), "discord_id": str(me["id"])}
    db.upsert_user(user["id"], user["name"])
    request.session["user"] = user
    return RedirectResponse(_home(request))


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(_home(request))


# ---------------- API: read ----------------
@app.get("/api/trips")
async def get_trips(request: Request):
    user = auth.current_user(request)
    if not config.OPEN_BOARD and not user:
        raise HTTPException(401, "login required")
    trips = await lifecycle.active_trips()
    # Anonymise owner ids before they leave the server. `me` is hashed with the
    # same function so the frontend's "is this my trip?" comparison still holds.
    for t in trips:
        t["owner_id"] = public_owner_id(t["owner_id"])
    return JSONResponse({
        "trips": trips,
        "me": public_owner_id(user["id"]) if user else None,
        "is_admin": auth.is_admin(user),
    })


@app.get("/api/airports/search")
def airport_search(q: str = Query("")):
    return {"results": db.search_airports(q)}


# ---------------- API: write ----------------
async def _build_legs(payload: dict) -> list[dict]:
    items: list[tuple] = []
    for direction in ("out", "ret"):
        for i, item in enumerate(payload.get(direction) or []):
            # Clip each field so an oversized payload can't bloat the DB or the
            # board feed. Generous vs real values (codes/flight numbers are short).
            flight_no = (item.get("flight_no") or "").strip()[:12]
            date_local = (item.get("date") or "").strip()[:10]
            mfrom = ((item.get("from") or "").strip()[:10]) or None
            mto = ((item.get("to") or "").strip()[:10]) or None
            if not flight_no and not (mfrom and mto):
                continue
            items.append((direction, i, flight_no, date_local, mfrom, mto))
    # Cap before resolving, so an oversized request can't spend API calls.
    if len(items) > config.MAX_LEGS_PER_TRIP:
        raise HTTPException(400, f"too many legs (max {config.MAX_LEGS_PER_TRIP} per trip)")
    legs: list[dict] = []
    for direction, i, flight_no, date_local, mfrom, mto in items:
        leg = await resolver.resolve_leg(
            direction, i, flight_no, date_local,
            manual_from=mfrom, manual_to=mto,
        )
        legs.append(leg)
    return legs


_LEG_COLS = ("trip_id", "direction", "seq", "date_local", "flight_no", "callsign",
             "dep_icao", "dep_iata", "dep_name", "dep_lat", "dep_lon", "dep_local", "dep_utc",
             "arr_icao", "arr_iata", "arr_name", "arr_lat", "arr_lon", "arr_local", "arr_utc",
             "reg", "ac_type", "ac_model", "ac_age", "ac_built", "resolved", "manual")

# Leg data carried over from a previous version of the leg when a re-resolve
# fails: everything except identity/order (direction, seq, flight_no,
# date_local) and the manual flag.
_PRESERVE_FIELDS = ("callsign",
                    "dep_icao", "dep_iata", "dep_name", "dep_lat", "dep_lon", "dep_local", "dep_utc",
                    "arr_icao", "arr_iata", "arr_name", "arr_lat", "arr_lon", "arr_local", "arr_utc",
                    "reg", "ac_type", "ac_model", "ac_age", "ac_built", "resolved")


def _preserve_resolved(new_legs: list[dict], old_legs: list[dict]) -> None:
    """On edit, if re-resolving a leg failed (API outage, expired cache) but its
    flight number and date are unchanged, keep the previously resolved data
    instead of silently downgrading the leg to unresolved."""
    old = {}
    for l in old_legs:
        if l.get("resolved") and l.get("flight_no"):
            old[(l["flight_no"], l.get("date_local"))] = l
    for leg in new_legs:
        if leg.get("resolved") or leg.get("manual") or not leg.get("flight_no"):
            continue
        prev = old.get((leg["flight_no"], leg.get("date_local")))
        if prev:
            for f in _PRESERVE_FIELDS:
                leg[f] = prev[f]


def _insert_legs(conn, trip_id: int, legs: list[dict]) -> None:
    for leg in legs:
        leg["trip_id"] = trip_id
        conn.execute(
            f"INSERT INTO legs ({','.join(_LEG_COLS)}) VALUES ({','.join('?' * len(_LEG_COLS))})",
            tuple(leg.get(c) for c in _LEG_COLS),
        )


def _insert_trip(owner_id: str, owner_name: str, tz: str | None, legs: list[dict],
                 manage_token_hash: str | None, ends_at: int | None) -> int:
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trips (owner_id, owner_name, submitter_tz, created_at, manage_token, ends_at) "
            "VALUES (?,?,?,?,?,?)",
            (owner_id, owner_name, tz, int(time.time()), manage_token_hash, ends_at),
        )
        trip_id = cur.lastrowid
        _insert_legs(conn, trip_id, legs)
    return trip_id


@app.post("/api/trips")
async def create_trip(request: Request):
    _check_write_rate(request)
    user = auth.current_user(request)
    payload = await request.json()
    tz = payload.get("tz")

    # Identity: a logged-in Discord user, or a name-only manual submitter.
    manage_token = None
    manage_hash = None
    if user:
        owner_id, owner_name = user["id"], user["name"]
        if tz:
            db.upsert_user(owner_id, owner_name, tz)
    else:
        if not config.OPEN_BOARD:
            raise HTTPException(401, "login required")
        owner_name = (payload.get("name") or "").strip()[:MANAGE_NAME_MAX]
        if not owner_name:
            raise HTTPException(400, "a name is required to post without logging in")
        uid = payload.get("uid")
        if isinstance(uid, str) and uid.startswith("m_") and _verify_uid_proof(uid, payload.get("proof")):
            owner_id = uid
        else:
            owner_id = _new_manual_uid()
        # Anonymous posters get a secret token (stored hashed) that lets the
        # browser edit/remove this trip later without an account.
        manage_token = secrets.token_urlsafe(24)
        manage_hash = _hash_token(manage_token)

    legs = await _build_legs(payload)
    if not legs:
        raise HTTPException(400, "no valid legs provided")
    ends_at = lifecycle.compute_ends_at(legs)
    trip_id = _insert_trip(owner_id, owner_name, tz, legs, manage_hash, ends_at)
    resp = {"ok": True, "trip_id": trip_id}
    if manage_token:
        # Returned exactly once; the browser stores it (and the uid) locally.
        resp.update(manage_token=manage_token, uid=owner_id)
    return resp


@app.put("/api/trips/{trip_id}")
async def edit_trip(request: Request, trip_id: int,
                    x_manage_token: str | None = Header(default=None)):
    _check_write_rate(request)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        old_legs = [dict(l) for l in conn.execute(
            "SELECT * FROM legs WHERE trip_id = ?", (trip_id,)).fetchall()]
    if not row:
        raise HTTPException(404, "not found")
    _authorize_manage(request, row, x_manage_token)

    payload = await request.json()
    legs = await _build_legs(payload)
    if not legs:
        raise HTTPException(400, "no valid legs provided")
    _preserve_resolved(legs, old_legs)
    ends_at = lifecycle.compute_ends_at(legs)
    tz = payload.get("tz") or row["submitter_tz"]
    with db.get_conn() as conn:
        conn.execute("DELETE FROM legs WHERE trip_id = ?", (trip_id,))
        conn.execute("UPDATE trips SET submitter_tz = ?, ends_at = ? WHERE id = ?",
                     (tz, ends_at, trip_id))
        _insert_legs(conn, trip_id, legs)
    return {"ok": True, "trip_id": trip_id}


@app.delete("/api/trips/{trip_id}")
def delete_trip(request: Request, trip_id: int,
                x_manage_token: str | None = Header(default=None)):
    _check_write_rate(request)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        _authorize_manage(request, row, x_manage_token)
        conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    return {"ok": True}
