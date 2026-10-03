"""End to end through the REAL resolver and preview router (only the upstream HTTP
is mocked): create, board payload, unverified fallback, and the preview contract."""

import json

import httpx
import pytest
from fixtures.loader import FIX, future_date

from app import aerodatabox as adb
from app import config, db

D = future_date(10)


def _cx271(date: str) -> str:
    nxt = future_date(11)
    text = (FIX / "real_flight_cx271.json").read_text()
    return text.replace("2025-01-11", date).replace("2025-01-12", nxt)


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(config, "AERODATABOX_KEY", "test-key")
    with db.get_conn() as c:
        for ident, iata, name, lat, lon in [
            ("VHHH", "HKG", "Hong Kong Intl", 22.31, 113.91),
            ("EHAM", "AMS", "Amsterdam Schiphol", 52.31, 4.76),
        ]:
            c.execute(
                "INSERT OR REPLACE INTO airports (ident,iata,name,lat,lon,type,municipality) "
                "VALUES (?,?,?,?,?,?,?)",
                (ident, iata, name, lat, lon, "large_airport", name),
            )
    state = {"flights": httpx.Response(200, json=json.loads(_cx271(D)))}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/aircrafts/" in request.url.path:
            return httpx.Response(204)
        return state["flights"]

    adb.set_transport(httpx.MockTransport(handler))
    yield state
    adb.set_transport(None)


def post(client, **body):
    body.setdefault("name", "Alex")
    return client.post("/api/trips", json=body, headers={"CF-Connecting-IP": "10.9.0.1"})


def test_flight_row_resolves_and_shows_on_board(client, upstream):
    r = post(client, out=[{"flight_no": "CX 271", "date": D}])
    assert r.status_code == 200, r.text
    leg = client.get("/api/trips").json()["trips"][0]["out"][0]
    assert (leg["from"], leg["to"]) == ("VHHH", "EHAM")
    assert leg["resolved"] is True and leg["unverified"] is False
    assert leg["flight_no"] == "CX271" and leg["arr_utc"]
    assert leg["live_state"] is None
    assert leg["fa_url"] == "https://flightaware.com/live/flight/" + (leg["callsign"] or "CX271")


def test_unknown_flight_is_rejected_then_kept_when_accepted(client, upstream):
    upstream["flights"] = httpx.Response(204)
    r = post(client, out=[{"flight_no": "ZZ999", "date": D}])
    assert r.status_code == 400
    rows = r.json()["detail"]["rows"]
    assert rows[0]["status"] == "not_found" and rows[0]["message"]
    r = post(client, out=[{"flight_no": "ZZ999", "date": D}], accept_unverified=True)
    assert r.status_code == 200
    leg = client.get("/api/trips").json()["trips"][0]["out"][0]
    assert leg["unverified"] is True and leg["resolved"] is False


def test_missing_date_defaults_to_today_and_never_fails(client, upstream):
    upstream["flights"] = httpx.Response(204)
    r = post(client, out=[{"flight_no": "ZZ999"}])
    assert r.status_code == 400 and r.json()["detail"]["rows"][0]["status"] == "not_found"


def test_preview_contract(client, upstream):
    r = client.post(
        "/api/legs/preview",
        headers={"CF-Connecting-IP": "10.9.0.2"},
        json={
            "rows": [
                {"kind": "flight", "flight_no": "CX271", "date": D},
                {"kind": "flight", "flight_no": "nonsense", "date": D},
            ]
        },
    )
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["status"] == "ok" and res[0]["leg"]["to"] == "EHAM"
    assert res[1]["status"] == "invalid" and res[1]["message"]


def test_malformed_preview_body_is_400(client):
    r = client.post("/api/legs/preview", content="nope", headers={"content-type": "application/json"})
    assert r.status_code == 400
