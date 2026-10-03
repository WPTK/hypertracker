"""POST /api/legs/preview on a throwaway FastAPI app. Offline."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, preview
from fixtures.loader import Upstream, env, future_date, resp  # noqa: F401
from test_resolver import Router, aircraft_ok, multileg, single  # noqa: F401

D = future_date(10)
LEG_KEYS = {
    "direction", "seq", "date_local", "flight_no", "callsign",
    "from", "from_iata", "from_name", "from_city", "from_lat", "from_lon",
    "to", "to_iata", "to_name", "to_city", "to_lat", "to_lon",
    "dep_local", "arr_local", "dep_utc", "arr_utc",
    "reg", "ac_type", "ac_model", "ac_age", "ac_built",
    "resolved", "manual", "unverified", "live_state", "fa_url",
}


@pytest.fixture
def api(env, monkeypatch):
    preview.reset_rate_limit()
    monkeypatch.setattr(config, "MAX_LEGS_PER_TRIP", 4)
    app = FastAPI()
    app.include_router(preview.router)
    with TestClient(app) as c:
        yield c
    preview.reset_rate_limit()


def post(api, rows, **kw):
    return api.post("/api/legs/preview", json={"rows": rows}, **kw)


def flight(no="DL1200", **kw):
    return {"kind": "flight", "flight_no": no, "date": D, **kw}


def test_flight_ok_full_leg_shape(api, env):
    env.use(Router({"DL1200": single}, aircraft_ok))
    r = post(api, [flight(" dl-1200 ")])
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["index"] == 0 and res["status"] == "ok" and res["candidates"] == []
    leg = res["leg"]
    assert set(leg) == LEG_KEYS
    assert leg["flight_no"] == "DL1200" and leg["from"] == "KATL" and leg["to_iata"] == "DEN"
    assert leg["from_city"] == "Atlanta" and leg["to_city"] == "Denver"
    assert leg["resolved"] is True and leg["manual"] is False and leg["unverified"] is False
    assert leg["live_state"] is None
    assert leg["fa_url"] == "https://flightaware.com/live/flight/DAL1200"
    assert leg["ac_type"] == "A359" and leg["dep_utc"].endswith("Z")
    assert "message" in res and res["message"]


def test_manual_ok_and_no_upstream_call(api, env):
    up = env.use(Router())
    res = post(api, [{"kind": "manual", "from": "jax", "to": "KDEN", "date": D}]).json()["results"][0]
    assert res["status"] == "manual_ok" and up.calls_n == 0
    assert res["leg"]["manual"] is True and res["leg"]["resolved"] is False
    assert res["leg"]["flight_no"] is None and res["leg"]["fa_url"] is None


def test_mixed_rows_keep_order_and_index(api, env):
    env.use(Router({"DL1200": single}))
    rows = [
        flight("DL1200"),
        {"kind": "manual", "from": "JAX", "to": "XYZ"},
        flight("NOPE!"),
        flight("DL1999"),
        {"kind": "manual", "from": "JAX", "to": "DEN", "date": D},
    ]
    res = post(api, rows).json()["results"]
    assert [x["index"] for x in res] == [0, 1, 2, 3, 4]
    assert [x["status"] for x in res] == ["ok", "airport_unknown", "invalid", "not_found", "manual_ok"]
    assert res[1]["message"] == "I don't know the airport XYZ." and res[1]["leg"] is None


@pytest.mark.parametrize("row,status", [
    ({"kind": "flight"}, "invalid"),
    ({"kind": "flight", "flight_no": ""}, "invalid"),
    ({"kind": "flight", "flight_no": "1234567"}, "invalid"),
    ({"kind": "flight", "flight_no": "../../x"}, "invalid"),
    ({"kind": "flight", "flight_no": "DL1200", "date": "not-a-date"}, "invalid"),
    ({"kind": "flight", "flight_no": "DL1200", "date": "1999-01-01"}, "out_of_window"),
    ({"kind": "flight", "flight_no": "DL1200", "date": future_date(400)}, "out_of_window"),
    ({"kind": "flight", "flight_no": "DL1200", "from": "!!"}, "invalid"),
    ({"kind": "manual", "from": "JAX"}, "invalid"),
    ({"kind": "manual", "to": "JAX"}, "invalid"),
    ({"kind": "manual", "from": "JAX", "to": "jax"}, "invalid"),
    ({"kind": "manual", "from": "J", "to": "DEN"}, "invalid"),
])
def test_invalid_rows_never_reach_upstream(api, env, row, status):
    up = env.use(Router({"DL1200": single}))
    res = post(api, [row]).json()["results"][0]
    assert res["status"] == status and res["leg"] is None and res["message"]
    assert up.calls_n == 0


def test_missing_date_defaults_to_today(api, env):
    from fixtures.loader import today
    env.use(Router())
    res = post(api, [{"kind": "flight", "flight_no": "DL1200"}]).json()["results"][0]
    assert res["status"] == "not_found" and today().strftime("%b") in res["message"]


def test_ambiguous_then_repreview_with_pick(api, env):
    env.use(Router({"AA300": multileg}))
    res = post(api, [flight("AA300")]).json()["results"][0]
    assert res["status"] == "ambiguous" and res["leg"] is None and len(res["candidates"]) == 2
    pick = res["candidates"][1]
    res2 = post(api, [flight("AA300", **{"from": pick["from"], "to": pick["to"]})]).json()["results"][0]
    assert res2["status"] == "ok" and res2["leg"]["from"] == "KATL" and res2["leg"]["to"] == "KDEN"


@pytest.mark.parametrize("item,status", [
    ((429, {"message": "x"}), "quota"),
    ((500, {"message": "x"}), "upstream_unavailable"),
    ((401, {"message": "x"}), "upstream_unavailable"),
    (httpx.ReadTimeout("t"), "upstream_unavailable"),
])
def test_upstream_trouble_is_a_status_not_an_error(api, env, item, status):
    env.use(Upstream(item))
    r = post(api, [flight()])
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["status"] == status and res["leg"] is None


# --- bad bodies are 400 with the contract error shape ----------------------
@pytest.mark.parametrize("body", [
    "not json", "[]", "null", "42", '{"rows": "x"}', '{}', '{"rows": [1]}',
    '{"rows": [{"kind": "bogus"}]}', '{"rows": [{"kind": "flight", "flight_no": 12}]}',
    '{"rows": [{"kind": "flight", "flight_no": "' + "A" * 500 + '"}]}',
    '{"rows": [{"kind": "flight", "from": ["JAX"]}]}', '{"rows": [{}]}',
])
def test_malformed_bodies_are_400(api, body):
    r = api.post("/api/legs/preview", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_max_rows_is_twice_max_legs(api, env):
    env.use(Router())
    assert post(api, [{"kind": "manual", "from": "JAX", "to": "DEN"}] * 8).status_code == 200
    r = post(api, [{"kind": "manual", "from": "JAX", "to": "DEN"}] * 9)
    assert r.status_code == 400 and "8" in r.json()["detail"]


def test_empty_rows(api):
    assert post(api, []).json() == {"results": []}


def test_unknown_extra_fields_are_ignored(api, env):
    env.use(Router())
    r = post(api, [{"kind": "manual", "from": "JAX", "to": "DEN", "bonus": 1}])
    assert r.status_code == 200


# --- rate limiting ---------------------------------------------------------
def test_rate_limit_per_ip_sliding_window(api, env, monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_RATE_LIMIT", 3, raising=False)
    monkeypatch.setattr(config, "PREVIEW_RATE_WINDOW", 100, raising=False)
    t = [1000.0]
    monkeypatch.setattr(preview, "_now", lambda: t[0])
    ip = ["1.1.1.1"]
    monkeypatch.setattr(preview, "client_ip", lambda request: ip[0])
    env.use(Router())
    row = [{"kind": "manual", "from": "JAX", "to": "DEN"}]
    for _ in range(3):
        assert post(api, row).status_code == 200
        t[0] += 10
    r = post(api, row)                                   # t=1030, first hit at 1000
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["detail"]
    ip[0] = "2.2.2.2"
    assert post(api, row).status_code == 200             # other IP unaffected
    ip[0] = "1.1.1.1"
    t[0] = 1101                                          # first hit slid out of the window
    assert post(api, row).status_code == 200
    preview.reset_rate_limit()
    t[0] = 1102
    for _ in range(3):
        assert post(api, row).status_code == 200


def test_rate_limit_check_precedes_body_parsing(api, monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_RATE_LIMIT", 1, raising=False)
    api.post("/api/legs/preview", content="junk")
    assert api.post("/api/legs/preview", content="junk").status_code == 429


def test_limiter_memory_is_bounded(monkeypatch):
    preview.reset_rate_limit()
    monkeypatch.setattr(preview, "_MAX_TRACKED_IPS", 10)
    t = [0.0]
    monkeypatch.setattr(preview, "_now", lambda: t[0])
    for i in range(30):
        preview._rate_limited(f"10.0.0.{i}")
    t[0] = 10_000
    preview._rate_limited("9.9.9.9")
    assert len(preview._hits) <= 2
    preview.reset_rate_limit()
