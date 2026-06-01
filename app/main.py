"""Hyperfixed Flight Tracker — web app (pages + JSON API + Discord OAuth)."""
import time
from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, auth, resolver, lifecycle

app = FastAPI(title="Hyperfixed Flight Tracker", root_path=config.ROOT_PATH)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


def _base(request: Request) -> str:
    return request.scope.get("root_path", "") or ""


def _home(request: Request, suffix: str = "/") -> str:
    return _base(request) + suffix


@app.on_event("startup")
def _startup():
    db.init_db()


# ---------------- pages ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = auth.current_user(request)
    ctx = {"user": user, "base_path": _base(request)}
    ctx["gated"] = not user and not config.PUBLIC_BOARD
    return templates.TemplateResponse(request, "board.html", ctx)


# ---------------- auth ----------------
@app.get("/login")
def login():
    return RedirectResponse(auth.login_url())


@app.get("/auth/callback")
async def callback(request: Request, code: str | None = None, error: str | None = None):
    if error or not code:
        return RedirectResponse(_home(request, "/?auth=denied"))
    try:
        info = await auth.exchange_code(code)
    except Exception:
        return RedirectResponse(_home(request, "/?auth=error"))
    if not auth.in_required_guild(info["guilds"]):
        return RedirectResponse(_home(request, "/?auth=not_member"))
    me = info["me"]
    user = {"id": str(me["id"]), "name": auth.display_name(me)}
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
    if not config.PUBLIC_BOARD and not auth.current_user(request):
        raise HTTPException(401, "login required")
    user = auth.current_user(request)
    trips = await lifecycle.active_trips()
    return JSONResponse({
        "trips": trips,
        "me": user["id"] if user else None,
    })


@app.get("/api/airports/search")
def airport_search(q: str = Query("")):
    return {"results": db.search_airports(q)}


# ---------------- API: write ----------------
async def _build_legs(payload: dict) -> list[dict]:
    legs: list[dict] = []
    for direction in ("out", "ret"):
        items = payload.get(direction) or []
        for i, item in enumerate(items):
            flight_no = (item.get("flight_no") or "").strip()
            date_local = (item.get("date") or "").strip()
            mfrom = (item.get("from") or "").strip() or None
            mto = (item.get("to") or "").strip() or None
            if not flight_no and not (mfrom and mto):
                continue
            leg = await resolver.resolve_leg(
                direction, i, flight_no, date_local,
                manual_from=mfrom, manual_to=mto,
            )
            legs.append(leg)
    return legs


def _insert_trip(owner_id: str, owner_name: str, tz: str | None, legs: list[dict]) -> int:
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trips (owner_id, owner_name, submitter_tz, created_at) VALUES (?,?,?,?)",
            (owner_id, owner_name, tz, int(time.time())),
        )
        trip_id = cur.lastrowid
        cols = ("trip_id", "direction", "seq", "date_local", "flight_no", "callsign",
                "dep_icao", "dep_iata", "dep_name", "dep_lat", "dep_lon", "dep_local", "dep_utc",
                "arr_icao", "arr_iata", "arr_name", "arr_lat", "arr_lon", "arr_local", "arr_utc",
                "reg", "ac_type", "ac_model", "ac_age", "ac_built", "resolved", "manual")
        for leg in legs:
            leg["trip_id"] = trip_id
            conn.execute(
                f"INSERT INTO legs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(leg.get(c) for c in cols),
            )
    return trip_id


@app.post("/api/trips")
async def create_trip(request: Request):
    user = auth.require_user(request)
    payload = await request.json()
    tz = payload.get("tz")
    if tz:
        db.upsert_user(user["id"], user["name"], tz)
    legs = await _build_legs(payload)
    if not legs:
        raise HTTPException(400, "no valid legs provided")
    trip_id = _insert_trip(user["id"], user["name"], tz, legs)
    return {"ok": True, "trip_id": trip_id}


@app.delete("/api/trips/{trip_id}")
def delete_trip(request: Request, trip_id: int):
    user = auth.require_user(request)
    with db.get_conn() as conn:
        row = conn.execute("SELECT owner_id FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        if row["owner_id"] != user["id"] and not config.DEV_MODE:
            raise HTTPException(403, "not your trip")
        conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    return {"ok": True}


# ---------------- API: bot ----------------
@app.post("/api/bot/trips")
async def bot_create_trip(request: Request, x_bot_token: str | None = Header(default=None)):
    if not x_bot_token or x_bot_token != config.BOT_SHARED_TOKEN:
        raise HTTPException(401, "bad bot token")
    payload = await request.json()
    u = payload.get("user") or {}
    owner_id, owner_name = str(u.get("id") or ""), (u.get("name") or "pilot")
    if not owner_id:
        raise HTTPException(400, "missing user")
    db.upsert_user(owner_id, owner_name, payload.get("tz"))
    legs = await _build_legs(payload)
    if not legs:
        raise HTTPException(400, "no valid legs")
    trip_id = _insert_trip(owner_id, owner_name, payload.get("tz"), legs)
    # Return a compact summary the bot can echo back to the channel.
    summary = []
    for leg in legs:
        summary.append({
            "flight_no": leg["flight_no"],
            "from": leg["dep_icao"], "to": leg["arr_icao"],
            "ac_model": leg["ac_model"], "ac_age": leg["ac_age"],
            "resolved": bool(leg["resolved"]),
        })
    return {"ok": True, "trip_id": trip_id, "legs": summary}
