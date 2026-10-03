"""Tiny harness app for the shell e2e tests.

Renders the real app/templates/board.html, serves the real app/static dir
(with throwaway stubs for the modules other agents own layered on top), and
answers /api/trips with canned, controllable JSON. It deliberately does not
import app.main, so it needs no database or secrets.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import time
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "app" / "static"
STUBS = Path(__file__).resolve().parent / "stubs"
STUBBED = {"js/identity.js", "js/tripForm.js", "js/map.js", "css/map.css", "css/form.css"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
)

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(ROOT / "app" / "templates")),
    autoescape=jinja2.select_autoescape(["html"]),
)

STATE: dict = {"mode": "full", "fail": False, "version": 0, "log": [], "user": None, "admin": False}


def _fmt(epoch: float, off_h: int, local: bool) -> str:
    t = time.gmtime(epoch + off_h * 3600)
    s = time.strftime("%Y-%m-%d %H:%M", t)
    if not local:
        return s + "Z"
    sign = "-" if off_h < 0 else "+"
    return f"{s}{sign}{abs(off_h):02d}:00"


def leg(direction="out", seq=0, **kw):
    base = {k: None for k in (
        "date_local flight_no callsign from from_iata from_name from_city from_lat from_lon "
        "to to_iata to_name to_city to_lat to_lon dep_local arr_local dep_utc arr_utc reg ac_type "
        "ac_model ac_age ac_built live_state fa_url").split()}
    base.update(direction=direction, seq=seq, resolved=False, manual=False, unverified=False)
    base.update(kw)
    return base


def flight(direction, seq, fno, frm, to, dep_off_s, dur_s, dep_tz, arr_tz, now, **kw):
    dep = now + dep_off_s
    arr = dep + dur_s
    airports = {
        "KDEN": ("DEN", "Denver International", "Denver"),
        "KJAX": ("JAX", "Jacksonville International", "Jacksonville"),
        "EGLL": ("LHR", "Heathrow", "London"),
        "KJFK": ("JFK", "John F Kennedy International", "New York"),
        "KATL": ("ATL", "Hartsfield-Jackson Atlanta International", "Atlanta"),
    }
    fi, fn, fc = airports[frm]
    ti, tn, tc = airports[to]
    d = dict(
        direction=direction, seq=seq, flight_no=fno, callsign=fno.replace("DL", "DAL").replace("UA", "UAL"),
        date_local=_fmt(dep, dep_tz, True)[:10],
        resolved=True, **{"from": frm}, from_iata=fi, from_name=fn, from_city=fc, to=to, to_iata=ti, to_name=tn, to_city=tc,
        dep_local=_fmt(dep, dep_tz, True), arr_local=_fmt(arr, arr_tz, True),
        dep_utc=_fmt(dep, 0, False), arr_utc=_fmt(arr, 0, False),
        fa_url=f"https://flightaware.com/live/flight/{fno}",
    )
    d.update(kw)
    return leg(**d)


def payload(mode: str):
    now = STATE.setdefault("t0", time.time())   # frozen so the ETag is stable
    H = 3600
    alex_out = flight("out", 0, "DL1200", "KDEN", "KJAX", -1 * H, 3 * H, -6, -4, now, live_state="airborne",
                      reg="N841DN", ac_type="B739", ac_model="Boeing 737-900", ac_age=6)
    alex_ret = flight("ret", 0, "DL1201", "KJAX", "KDEN", 26 * H, 3 * H, -4, -6, now,
                      reg=None, ac_model="Boeing 737-900")
    alex2_a = flight("out", 0, "UA100", "EGLL", "KATL", 30 * H, 9 * H, 1, -4, now)
    alex2_b = flight("out", 1, "DL1200", "KATL", "KDEN", 30 * H + 10 * H, 4 * H, -4, -6, now)
    bailey = flight("out", 0, "UA100", "EGLL", "KJFK", 40 * 60, 8 * H, 1, -4, now, live_state="on_ground",
                    ac_model=None)
    casey = leg("out", 0, flight_no="XX999", callsign="XX999", date_local=_fmt(now + 5 * H, 0, True)[:10],
                unverified=True, fa_url="https://flightaware.com/live/flight/XX999")
    dana = leg("out", 0, date_local=_fmt(now + 2 * 86400, 0, True)[:10], **{"from": "KDEN"}, from_iata="DEN",
               from_city="Denver", to="KJAX", to_iata="JAX", to_city="Jacksonville", manual=True, resolved=True,
               from_name="Denver International", to_name="Jacksonville International")
    eli = flight("out", 0, "DL1200", "KJAX", "KATL", -6 * H, 1 * H, -4, -4, now)
    trips = [
        {"id": 1, "owner_id": "o-alex", "owner_name": "Alex", "out": [alex_out], "ret": [alex_ret]},
        {"id": 2, "owner_id": "o-alex", "owner_name": "Alex", "out": [alex2_a, alex2_b], "ret": []},
        {"id": 3, "owner_id": "o-bailey", "owner_name": "Bailey", "out": [bailey], "ret": []},
        {"id": 4, "owner_id": "o-casey", "owner_name": "Casey", "out": [casey], "ret": []},
        {"id": 5, "owner_id": "o-dana", "owner_name": "Dana", "out": [dana], "ret": []},
        {"id": 6, "owner_id": "o-eli", "owner_name": "Eli", "out": [eli], "ret": []},
    ]
    if mode == "empty":
        trips = []
    elif mode == "extra":
        trips.append({"id": 7, "owner_id": "o-fay", "owner_name": "Fay", "out": [
            flight("out", 0, "DL77", "KATL", "KDEN", 8 * H, 3 * H, -4, -6, now)], "ret": []})
    elif mode == "no_air":
        alex_out["live_state"] = None
    return {
        "trips": trips, "me": "o-alex", "is_admin": STATE["admin"],
        "server_time": int(time.time()), "live_updated_at": int(time.time()) - 30,
    }


app = FastAPI()


@app.middleware("http")
async def csp(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    return resp


def render(gated=False):
    user = STATE["user"]
    return env.get_template("board.html").render(
        user=user, is_admin=STATE["admin"], discord_enabled=True, gated=gated, base_path="")


@app.get("/", response_class=HTMLResponse)
def index():
    return render()


@app.get("/gated", response_class=HTMLResponse)
def gated():
    return render(gated=True)


@app.get("/static/{path:path}")
def static(path: str):
    if path in STUBBED:
        f = STUBS / path
    else:
        f = (STATIC / path)
    f = f.resolve()
    if not f.is_file() or not (str(f).startswith(str(STATIC.resolve())) or str(f).startswith(str(STUBS.resolve()))):
        return Response(status_code=404)
    mt = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
    if f.suffix == ".js":
        mt = "text/javascript"
    return FileResponse(f, media_type=mt, headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login():
    return Response(status_code=204)


@app.post("/logout")
def logout():
    return Response(status_code=204)


@app.get("/api/trips")
def trips(request: Request):
    STATE["log"].append({"path": "/api/trips", "inm": request.headers.get("if-none-match")})
    if STATE["fail"]:
        return JSONResponse({"detail": "down"}, status_code=503)
    body = payload(STATE["mode"])
    # ETag ignores server_time and live_updated_at, as the real endpoint would.
    stable = dict(body, server_time=0, live_updated_at=0)
    etag = '"' + hashlib.md5(json.dumps(stable, sort_keys=True).encode()).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(body, headers={"ETag": etag})


@app.post("/__test/set")
async def set_state(request: Request):
    STATE.update(await request.json())
    return {"ok": True}


@app.get("/__test/log")
def get_log():
    return STATE["log"]
