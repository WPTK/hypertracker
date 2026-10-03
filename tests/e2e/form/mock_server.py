"""Tiny mock of the trip API for the form e2e tests.

Implements POST /api/legs/preview, GET/POST /api/trips, PUT/DELETE
/api/trips/{id} per docs/api-contract.md, with canned statuses driven by the
flight number typed. Also serves the harness page and the real static modules.
`/static/js/api.js` is the harness stub so the tests do not depend on the shell
agent's implementation.

Flight numbers:
  DL1200 ok (JAX to DEN)     DL1201 ok (ATL to LAX)     DL1202 ok (LAX to ATL)
  DL5555 ok, but POST /api/trips takes 1.2s (busy-state tests)
  AA100  ambiguous, two candidates; ok once From/To pick one
  DL9999 not_found           DL7777 invalid           DL8888 out_of_window
  UA1    upstream_unavailable    BA1 quota
  any other well-formed number: not_found
Manual rows: ok if both airports are known, else airport_unknown.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE.parents[2] / "app" / "static"

AIRPORTS = {
    "KJAX": ("JAX", "Jacksonville Intl", "Jacksonville"),
    "KDEN": ("DEN", "Denver Intl", "Denver"),
    "KATL": ("ATL", "Hartsfield-Jackson", "Atlanta"),
    "KLAX": ("LAX", "Los Angeles Intl", "Los Angeles"),
    "KORD": ("ORD", "O'Hare Intl", "Chicago"),
    "KJFK": ("JFK", "John F Kennedy Intl", "New York"),
    "EGLL": ("LHR", "Heathrow", "London"),
}
IATA_TO_ICAO = {v[0]: k for k, v in AIRPORTS.items()}

FLIGHTS = {
    "DL1200": ("KJAX", "KDEN", "08:15", "10:05", "Boeing 737-800"),
    "DL5555": ("KJAX", "KDEN", "08:15", "10:05", "Boeing 737-800"),
    "DL1201": ("KATL", "KLAX", "12:00", "14:30", "Airbus A321"),
    "DL1202": ("KLAX", "KATL", "16:00", "23:40", "Airbus A321"),
}
AMBIGUOUS = {
    "AA100": [("KJAX", "KATL", "07:00", "08:20"), ("KATL", "KDEN", "11:10", "12:40")],
}
STATUS_ONLY = {
    "DL9999": (
        "not_found",
        "I couldn't find DL9999 on that date. Check the date, or add the airports yourself.",
    ),
    "DL7777": ("invalid", "DL7777 isn't a flight number I can look up."),
    "DL8888": ("out_of_window", "That date is outside the range I can check."),
    "UA1": (
        "upstream_unavailable",
        "The flight data service isn't answering right now. I can keep it unverified.",
    ),
    "BA1": ("quota", "I've used up today's flight lookups. I can keep it unverified."),
}
KEEPABLE = {"not_found", "upstream_unavailable", "quota"}


def norm(s) -> str:
    return "".join(str(s or "").upper().replace("-", "").split())


def icao(code: str) -> str | None:
    code = norm(code)
    if code in AIRPORTS:
        return code
    return IATA_TO_ICAO.get(code)


def leg_for(
    direction: str,
    seq: int,
    date: str,
    flight_no: str | None,
    f: str,
    t: str,
    dep: str | None = None,
    arr: str | None = None,
    model: str | None = None,
    unverified: bool = False,
) -> dict:
    fa, ta = AIRPORTS.get(f), AIRPORTS.get(t)
    leg = {
        "direction": direction,
        "seq": seq,
        "date_local": date,
        "flight_no": flight_no or None,
        "callsign": None,
        "from": f,
        "from_iata": fa[0] if fa else None,
        "from_name": fa[1] if fa else None,
        "from_city": fa[2] if fa else None,
        "from_lat": None,
        "from_lon": None,
        "to": t,
        "to_iata": ta[0] if ta else None,
        "to_name": ta[1] if ta else None,
        "to_city": ta[2] if ta else None,
        "to_lat": None,
        "to_lon": None,
        "dep_local": f"{date} {dep}-04:00" if dep else None,
        "arr_local": f"{date} {arr}-06:00" if arr else None,
        "dep_utc": None,
        "arr_utc": None,
        "reg": None,
        "ac_type": None,
        "ac_model": model,
        "ac_age": None,
        "ac_built": None,
        "resolved": bool(flight_no) and not unverified,
        "manual": not flight_no,
        "unverified": unverified,
        "live_state": None,
        "fa_url": None,
    }
    return leg


def check_row(row: dict, direction="out", seq=0, kind=None) -> dict:
    """Return {status, message, leg, candidates} for one input row."""
    date = row.get("date") or time.strftime("%Y-%m-%d", time.gmtime())
    fn = norm(row.get("flight_no"))
    kind = kind or ("flight" if fn else "manual")
    if kind == "manual" or not fn:
        f, t = icao(row.get("from") or ""), icao(row.get("to") or "")
        bad = [c for c, i in ((row.get("from"), f), (row.get("to"), t)) if not i]
        if not row.get("from") or not row.get("to") or bad:
            name = norm(bad[0]) if bad else "that airport"
            return {
                "status": "airport_unknown",
                "message": f"I don't know the airport {name}.",
                "leg": None,
                "candidates": [],
            }
        return {
            "status": "manual_ok",
            "message": "Both airports check out.",
            "leg": leg_for(direction, seq, date, None, f, t),
            "candidates": [],
        }

    if fn in STATUS_ONLY:
        st, msg = STATUS_ONLY[fn]
        return {"status": st, "message": msg, "leg": None, "candidates": []}
    if fn in AMBIGUOUS:
        cands = [
            {
                "from": a,
                "from_iata": AIRPORTS[a][0],
                "to": b,
                "to_iata": AIRPORTS[b][0],
                "dep_local": f"{date} {d}-04:00",
                "arr_local": f"{date} {r}-04:00",
            }
            for a, b, d, r in AMBIGUOUS[fn]
        ]
        hf, ht = icao(row.get("from") or "") or "", icao(row.get("to") or "") or ""
        picked = [c for c in cands if (not hf or c["from"] == hf) and (not ht or c["to"] == ht)]
        if (hf or ht) and len(picked) == 1:
            c = picked[0]
            return {
                "status": "ok",
                "message": "Found it.",
                "leg": leg_for(
                    direction,
                    seq,
                    date,
                    fn,
                    c["from"],
                    c["to"],
                    c["dep_local"][11:16],
                    c["arr_local"][11:16],
                    "Boeing 737-800",
                ),
                "candidates": [],
            }
        return {
            "status": "ambiguous",
            "message": f"{fn} flies two legs that day. Pick yours.",
            "leg": None,
            "candidates": cands,
        }
    if fn in FLIGHTS:
        f, t, dep, arr, model = FLIGHTS[fn]
        return {
            "status": "ok",
            "message": "Found it.",
            "leg": leg_for(direction, seq, date, fn, f, t, dep, arr, model),
            "candidates": [],
        }
    return {
        "status": "not_found",
        "message": f"I couldn't find {fn} on that date. Check the date, or add the airports yourself.",
        "leg": None,
        "candidates": [],
    }


class State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.trips: dict[int, dict] = {}
        self.next_id = 1
        self.posts = 0
        self.puts = 0
        self.deletes = 0
        self.previews = 0
        self.last_post: dict | None = None
        self.last_put: dict | None = None
        self.last_put_headers: dict | None = None
        self.preview_log: list[dict] = []


S = State()
app = FastAPI()


def err(status: int, detail) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def logged_in(request: Request) -> bool:
    return request.cookies.get("testuser") == "1"


def public_trip(t: dict) -> dict:
    return {
        "id": t["id"],
        "owner_id": t["owner_id"],
        "owner_name": t["owner_name"],
        "out": t["out"],
        "ret": t["ret"],
    }


@app.get("/")
def index(user: str | None = None):
    resp = FileResponse(HERE / "harness" / "index.html")
    if user:
        resp.set_cookie("testuser", "1")
    else:
        resp.delete_cookie("testuser")
    return resp


@app.get("/harness/{name}")
def harness_file(name: str):
    return FileResponse(HERE / "harness" / name)


@app.get("/static/js/api.js")
def stub_api():
    return FileResponse(HERE / "harness" / "api.js", media_type="text/javascript")


@app.get("/api/trips")
def list_trips(request: Request):
    me = "u1" if logged_in(request) else None
    return {
        "trips": [public_trip(t) for t in S.trips.values()],
        "me": me,
        "is_admin": False,
        "server_time": int(time.time()),
        "live_updated_at": int(time.time()),
    }


@app.post("/api/legs/preview")
async def preview(request: Request):
    body = await request.json()
    rows = body.get("rows") or []
    S.previews += 1
    S.preview_log.append(body)
    results = []
    for i, row in enumerate(rows):
        r = check_row(row, kind=row.get("kind"))
        r["index"] = i
        results.append(r)
    return {"results": results}


def resolve_all(body: dict):
    accept = bool(body.get("accept_unverified"))
    legs = {"out": [], "ret": []}
    bad = []
    for direction in ("out", "ret"):
        for i, row in enumerate(body.get(direction) or []):
            r = check_row(row, direction, i)
            date = row.get("date") or time.strftime("%Y-%m-%d", time.gmtime())
            if r["status"] in ("ok", "manual_ok"):
                legs[direction].append(r["leg"])
            elif accept and r["status"] in KEEPABLE:
                fn = norm(row.get("flight_no"))
                legs[direction].append(
                    leg_for(
                        direction,
                        i,
                        date,
                        fn,
                        icao(row.get("from") or "") or "",
                        icao(row.get("to") or "") or "",
                        unverified=True,
                    )
                )
            else:
                bad.append(
                    {"direction": direction, "index": i, "status": r["status"], "message": r["message"]}
                )
    return legs, bad


@app.post("/api/trips")
async def create_trip(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err(400, "That request wasn't valid.")
    S.posts += 1
    S.last_post = body
    legs, bad = resolve_all(body)
    if any(row.get("flight_no") == "DL5555" for row in (body.get("out") or []) + (body.get("ret") or [])):
        await asyncio.sleep(1.2)
    if bad:
        return err(400, {"message": "I couldn't save that. Some legs need another look.", "rows": bad})
    if not legs["out"] and not legs["ret"]:
        return err(400, "Add at least one flight.")
    tid = S.next_id
    S.next_id += 1
    trip = {"id": tid, "out": legs["out"], "ret": legs["ret"], "token": None}
    resp = {"ok": True, "trip_id": tid}
    if logged_in(request):
        trip["owner_id"], trip["owner_name"] = "u1", "Alex"
    else:
        token = secrets.token_urlsafe(24)
        uid = body.get("uid") or "m_" + secrets.token_urlsafe(9)
        trip.update(owner_id=uid, owner_name=(body.get("name") or "Anon")[:40], token=token)
        resp.update(manage_token=token, uid=uid)
    S.trips[tid] = trip
    return resp


def authorise(request: Request, trip: dict) -> JSONResponse | None:
    if logged_in(request) and trip["owner_id"] == "u1":
        return None
    tok = request.headers.get("x-manage-token")
    if tok and trip.get("token") and secrets.compare_digest(tok, trip["token"]):
        return None
    return err(403, "You can't change that trip.")


@app.put("/api/trips/{trip_id}")
async def edit_trip(trip_id: int, request: Request):
    trip = S.trips.get(trip_id)
    if not trip:
        return err(404, "That trip isn't on the board.")
    denied = authorise(request, trip)
    if denied:
        return denied
    body = await request.json()
    S.puts += 1
    S.last_put = body
    S.last_put_headers = dict(request.headers)
    legs, bad = resolve_all(body)
    if bad:
        return err(400, {"message": "I couldn't save that. Some legs need another look.", "rows": bad})
    trip["out"], trip["ret"] = legs["out"], legs["ret"]
    return {"ok": True, "trip_id": trip_id}


@app.delete("/api/trips/{trip_id}")
def delete_trip(trip_id: int, request: Request):
    trip = S.trips.get(trip_id)
    if not trip:
        return err(404, "That trip isn't on the board.")
    denied = authorise(request, trip)
    if denied:
        return denied
    S.deletes += 1
    del S.trips[trip_id]
    return {"ok": True}


@app.post("/__reset")
def reset():
    S.reset()
    return {"ok": True}


@app.post("/__drop/{trip_id}")
def drop(trip_id: int):
    """Remove a trip behind the client's back (simulates purge or another device)."""
    S.trips.pop(trip_id, None)
    return {"ok": True}


@app.post("/__seed")
async def seed(request: Request):
    """Create a trip directly: {out, ret, name} -> {trip_id, token, uid}."""
    body = await request.json()
    legs, bad = resolve_all({**body, "accept_unverified": True})
    tid = S.next_id
    S.next_id += 1
    token = secrets.token_urlsafe(24)
    uid = "m_" + secrets.token_urlsafe(9)
    S.trips[tid] = {
        "id": tid,
        "out": legs["out"],
        "ret": legs["ret"],
        "token": token,
        "owner_id": uid,
        "owner_name": body.get("name", "Seed"),
    }
    return {"trip_id": tid, "token": token, "uid": uid}


@app.get("/__stats")
def stats():
    return {
        "posts": S.posts,
        "puts": S.puts,
        "deletes": S.deletes,
        "previews": S.previews,
        "last_post": S.last_post,
        "last_put": S.last_put,
        "preview_log": S.preview_log,
        "last_put_headers": S.last_put_headers,
        "trips": len(S.trips),
    }


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class Server:
    """Run the mock in a background thread on a free port."""

    def __init__(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("mock server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


if __name__ == "__main__":
    srv = Server()
    print(srv.url)
    srv.start()
    srv.thread.join()
