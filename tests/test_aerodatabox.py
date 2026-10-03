"""AeroDataBox client: payload validation, typed failures, retry, breaker,
caching, flight picking and aircraft mapping. Offline (httpx.MockTransport)."""
import asyncio
import datetime as dt
import logging

import httpx
import pytest

from app import aerodatabox as adb
from app import config, db
from fixtures.loader import Upstream, env, future_date, load, resp  # noqa: F401

run = asyncio.run
D = future_date(10)


def fetch(no="DL1200", date=D, **kw):
    async def go():
        try:
            return await adb.fetch_flights(no, date, **kw)
        finally:
            await adb.aclose()
    return run(go())


def raw_cache(key):
    w = db.cache_get("flight_cache", "cache_key", key, 10 ** 10)
    return w


# --- parse_adb_dt ----------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("2026-06-04 12:00Z", "2026-06-04T12:00:00+00:00"),
    ("2026-06-04 08:00-04:00", "2026-06-04T12:00:00+00:00"),
    ("2025-01-11 23:20+08:00", "2025-01-11T15:20:00+00:00"),
    ("2026-06-04 12:00", "2026-06-04T12:00:00+00:00"),
    (None, None), ("", None), ("junk", None), (12345, None), ("2026-13-45 99:00Z", None),
])
def test_parse_adb_dt(raw, expected):
    d = adb.parse_adb_dt(raw)
    assert (d.isoformat() if d else None) == expected


# --- _pick_flight tables ---------------------------------------------------
def F(dep, arr, dep_local, dep_utc=None, codeshare="IsOperator", d_iata=None, a_iata=None):
    return {"departure": {"airport": {"icao": dep, "iata": d_iata},
                          "scheduledTime": {"local": dep_local, "utc": dep_utc}},
            "arrival": {"airport": {"icao": arr, "iata": a_iata}, "scheduledTime": {}},
            "codeshareStatus": codeshare}


ATL_DEN = F("KATL", "KDEN", "2026-10-10 11:00-04:00", "2026-10-10 15:00Z", d_iata="ATL", a_iata="DEN")
JAX_ATL = F("KJAX", "KATL", "2026-10-10 08:00-04:00", "2026-10-10 12:00Z", d_iata="JAX", a_iata="ATL")
NEXT_DAY = F("KATL", "KDEN", "2026-10-11 11:00-04:00", "2026-10-11 15:00Z", d_iata="ATL", a_iata="DEN")


@pytest.mark.parametrize("flights,dep,arr,kind,reason", [
    ([], None, None, "not_found", "no_date_match"),
    ([NEXT_DAY], None, None, "not_found", "no_date_match"),             # no date fallback
    ([ATL_DEN], None, None, "found", None),
    ([ATL_DEN], "KATL", "KDEN", "found", None),
    ([ATL_DEN], "atl", "den", "found", None),                           # IATA, any case
    ([ATL_DEN], "KJAX", None, "not_found", "route_mismatch"),           # hint vs single candidate
    ([ATL_DEN], None, "LHR", "not_found", "route_mismatch"),
    ([ATL_DEN, JAX_ATL], None, None, "ambiguous", None),
    ([ATL_DEN, JAX_ATL], "JAX", None, "found", None),
    ([ATL_DEN, JAX_ATL], None, "KDEN", "found", None),
    ([ATL_DEN, JAX_ATL], "KATL", "KDEN", "found", None),
    ([ATL_DEN, JAX_ATL], "EGLL", None, "not_found", "route_mismatch"),
    ([ATL_DEN, JAX_ATL], "KATL", "KATL", "not_found", "route_mismatch"),
    ([ATL_DEN, JAX_ATL, NEXT_DAY], None, "ATL", "found", None),
    ([ATL_DEN, ATL_DEN | {"departure": {**ATL_DEN["departure"], "scheduledTime": {"local": "2026-10-10 18:00-04:00", "utc": "2026-10-10 22:00Z"}}}],
     "ATL", None, "ambiguous", None),                                   # hint matches several
])
def test_pick_table(flights, dep, arr, kind, reason):
    p = adb._pick_flight(flights, "2026-10-10", dep, arr)
    assert p.kind == kind
    assert p.reason == reason
    assert (p.flight is not None) == (kind == "found")


def test_pick_ambiguous_sorted_by_departure_utc():
    p = adb._pick_flight([ATL_DEN, JAX_ATL], "2026-10-10")
    assert [adb.airport_codes(f, "departure") & {"KJAX", "KATL"} for f in p.candidates] == [{"KJAX"}, {"KATL"}]


def test_pick_other_dates_for_message_only():
    p = adb._pick_flight([NEXT_DAY], "2026-10-10")
    assert p.other_dates == ["2026-10-11"] and p.flight is None


def test_pick_dedupes_codeshares_preferring_operator():
    cs = JAX_ATL | {"codeshareStatus": "IsCodeshared", "number": "UA 1"}
    p = adb._pick_flight([cs, JAX_ATL | {"number": "AA 1"}], "2026-10-10")
    assert p.kind == "found" and p.flight["number"] == "AA 1"


def test_pick_ignores_garbage_entries():
    assert adb._pick_flight([None, "x", ATL_DEN], "2026-10-10").kind == "found"


# --- fetching and typed outcomes -------------------------------------------
def test_ok_and_request_shape(env):
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    r = fetch()
    assert r.ok and len(r.flights) == 1 and not r.cached
    req = up.requests[0]
    assert req.url.path == f"/flights/Number/DL1200/{D}"
    q = dict(req.url.params)
    assert q["withLocation"] == "false" and q["withAircraftImage"] == "false"
    assert q["dateLocalRole"] == "Departure" and "withFlightPlan" not in q
    assert req.headers["X-RapidAPI-Key"] == "secret-test-key"
    assert req.headers["X-RapidAPI-Host"]


def test_apimarket_headers(env, monkeypatch):
    monkeypatch.setattr(config, "AERODATABOX_AUTH", "apimarket", raising=False)
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    fetch()
    h = up.requests[0].headers
    assert h["x-api-market-key"] == "secret-test-key"
    assert "x-rapidapi-key" not in h and "x-rapidapi-host" not in h


def test_apimarket_autodetected_from_base(env, monkeypatch):
    monkeypatch.setattr(config, "AERODATABOX_BASE", "https://prod.api.market/api/v1/aedbx/aerodatabox")
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    fetch()
    assert "x-api-market-key" in up.requests[0].headers
    assert up.requests[0].url.path.startswith("/api/v1/aedbx/aerodatabox/flights/Number/")


def test_path_segments_are_quoted_and_malformed_input_never_calls(env):
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    for bad in ("../../aircrafts/reg/N1", "DL 1200/x", "", "D%L1"):
        assert fetch(bad).kind == adb.KIND_NO_FLIGHTS
    assert fetch("DL1200", "2026-10-10/../x").kind == adb.KIND_NO_FLIGHTS
    assert up.calls == 0
    assert adb.quote("a/b c", safe="") == "a%2Fb%20c"


def test_no_key_is_bad_key_without_a_call(env, monkeypatch):
    monkeypatch.setattr(config, "AERODATABOX_KEY", "")
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    assert fetch().kind == adb.KIND_BAD_KEY and up.calls == 0


@pytest.mark.parametrize("script,kind,calls", [
    ((204, None), adb.KIND_NO_FLIGHTS, 1),
    ((404, {"message": "nope"}), adb.KIND_NO_FLIGHTS, 1),
    ((401, "error_401.json"), adb.KIND_BAD_KEY, 1),
    ((403, "error_401.json"), adb.KIND_BAD_KEY, 1),
    ((429, "error_429.json"), adb.KIND_QUOTA, 1),
    ((500, "error_500.json"), adb.KIND_DOWN, 2),       # one retry
    ((503, "error_500.json"), adb.KIND_DOWN, 2),
    ((400, {"message": "bad"}), adb.KIND_BAD_PAYLOAD, 1),
])
def test_status_mapping(env, script, kind, calls):
    status, body = script
    item = resp(status, body) if isinstance(body, str) else script
    up = env.use(Upstream(item))
    r = fetch()
    assert r.kind == kind and up.calls == calls
    assert r.flights == []


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.ConnectError("c")])
def test_timeout_and_network_retry_once_then_down(env, exc):
    up = env.use(Upstream(exc))
    r = fetch()
    assert r.kind == adb.KIND_DOWN and up.calls == 2
    assert len(env.sleeps) == 1 and 0 < env.sleeps[0] < 1   # jitter before the retry


def test_retry_recovers(env):
    up = env.use(Upstream(httpx.ReadTimeout("t"), resp(200, "flights_single.json", D)))
    assert fetch().ok and up.calls == 2
    up = env.use(Upstream((502, {"message": "x"}), resp(200, "flights_single.json", D)))
    assert fetch("DL1201").ok


@pytest.mark.parametrize("name", ["junk_message.json", "junk_null.json", "junk_string.json",
                                  "junk_items.json", "junk_no_airports.json"])
def test_junk_200_is_bad_payload_and_never_cached(env, name):
    up = env.use(Upstream(resp(200, name)))
    assert fetch().kind == adb.KIND_BAD_PAYLOAD
    assert fetch().kind == adb.KIND_BAD_PAYLOAD
    assert up.calls == 2                     # nothing cached
    assert raw_cache(f"v3|DL1200|{D}") is None


def test_dict_wrapped_flights_are_rejected(env):
    env.use(Upstream((200, {"flights": [{"departure": {}}]})))
    assert fetch().kind == adb.KIND_BAD_PAYLOAD


def test_not_json_body(env):
    env.use(Upstream(httpx.Response(200, text="<html>gateway</html>")))
    assert fetch().kind == adb.KIND_BAD_PAYLOAD


def test_errors_logged_at_warning_without_key(env, caplog):
    env.use(Upstream(resp(500, "error_500.json")))
    with caplog.at_level(logging.WARNING, logger="hypertracker.aerodatabox"):
        fetch()
    text = caplog.text
    assert "500" in text and "Internal server error" in text
    assert "secret-test-key" not in text
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


def test_log_body_truncated(env, caplog):
    env.use(Upstream(httpx.Response(500, text="x" * 5000)))
    with caplog.at_level(logging.WARNING, logger="hypertracker.aerodatabox"):
        fetch()
    assert "x" * 201 not in caplog.text and "x" * 200 in caplog.text


def test_low_units_header_warns(env, caplog):
    r = resp(200, "flights_single.json", D)
    r.headers["x-ratelimit-api-units-remaining"] = "12"
    env.use(Upstream(r))
    with caplog.at_level(logging.WARNING, logger="hypertracker.aerodatabox"):
        assert fetch().ok
    assert "units remaining" in caplog.text


# --- circuit breaker -------------------------------------------------------
@pytest.mark.parametrize("status,body,kind,seconds", [
    (401, "error_401.json", adb.KIND_BAD_KEY, 300), (403, "error_401.json", adb.KIND_BAD_KEY, 300),
    (429, "error_429.json", adb.KIND_QUOTA, 60),
])
def test_breaker_short_circuits_then_recovers(env, monkeypatch, status, body, kind, seconds):
    t = [1000.0]
    monkeypatch.setattr(adb, "_mono", lambda: t[0])
    up = env.use(Upstream(resp(status, body), resp(200, "flights_single.json", D)))
    assert fetch().kind == kind and up.calls == 1
    t[0] += seconds - 1
    assert fetch("DL1201").kind == kind and up.calls == 1       # no network
    assert adb.breaker_state()[0] == kind
    t[0] += 2
    assert fetch("DL1202").ok and up.calls == 2                  # closed again
    assert adb.breaker_state() == (None, 0.0)


def test_breaker_still_serves_cache(env, monkeypatch):
    up = env.use(Upstream(resp(200, "flights_single.json", D), resp(401, "error_401.json")))
    assert fetch().ok
    assert fetch("DL9999").kind == adb.KIND_BAD_KEY
    r = fetch()
    assert r.ok and r.cached and up.calls == 2


# --- caching ---------------------------------------------------------------
def test_cache_hit_avoids_second_call(env):
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    assert not fetch().cached
    r = fetch()
    assert r.ok and r.cached and up.calls == 1


def test_use_cache_false_bypasses_but_refreshes(env):
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    fetch()
    assert not fetch(use_cache=False).cached and up.calls == 2


def test_future_flights_get_near_ttl(env, monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_CACHE_TTL_NEAR", 21600, raising=False)
    monkeypatch.setattr(config, "FLIGHT_CACHE_TTL_PAST", 2592000, raising=False)
    now = [adb._now()]
    monkeypatch.setattr(adb, "_now", lambda: now[0])
    env.use(Upstream(resp(200, "flights_single.json", D)))
    fetch()
    w = raw_cache(f"v3|DL1200|{D}")
    assert w["_exp"] - now[0] == 21600 and not w.get("neg")


def test_completed_flights_get_past_ttl(env, monkeypatch):
    old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    env.use(Upstream(resp(200, "flights_single.json", old)))
    fetch(date=old)
    w = raw_cache(f"v3|DL1200|{old}")
    assert w["_exp"] - adb._now() == pytest.approx(2592000, abs=5)


def test_near_entry_expires(env, monkeypatch):
    now = [adb._now()]
    monkeypatch.setattr(adb, "_now", lambda: now[0])
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    fetch()
    now[0] += 21600 - 10
    assert fetch().cached
    now[0] += 20
    assert not fetch().cached and up.calls == 2


def test_negative_cache_marker_and_ttl(env, monkeypatch):
    now = [adb._now()]
    monkeypatch.setattr(adb, "_now", lambda: now[0])
    up = env.use(Upstream((204, None), resp(200, "flights_single.json", D)))
    assert fetch().kind == adb.KIND_NO_FLIGHTS
    w = raw_cache(f"v3|DL1200|{D}")
    assert w["neg"] is True and w["_exp"] - now[0] == 600
    r = fetch()
    assert r.kind == adb.KIND_NO_FLIGHTS and r.cached and up.calls == 1
    now[0] += 601
    assert fetch().ok and up.calls == 2


def test_empty_list_is_negative_cached(env):
    up = env.use(Upstream(resp(200, "flights_empty.json")))
    fetch(); fetch()
    assert up.calls == 1 and raw_cache(f"v3|DL1200|{D}")["neg"] is True


@pytest.mark.parametrize("item", [(500, {"message": "x"}), (429, {"message": "x"}),
                                  (401, {"message": "x"}), (200, {"message": "x"}),
                                  httpx.ReadTimeout("t")])
def test_errors_are_never_cached(env, item):
    env.use(Upstream(item))
    fetch()
    assert raw_cache(f"v3|DL1200|{D}") is None


def test_poisoned_cache_entry_is_refetched(env):
    db.cache_put("flight_cache", "cache_key", f"v3|DL1200|{D}",
                 {"_exp": adb._now() + 999, "data": {"message": "junk"}})
    up = env.use(Upstream(resp(200, "flights_single.json", D)))
    assert fetch().ok and up.calls == 1


# --- real-shape fixture ----------------------------------------------------
def test_real_cx271_payload_validates_and_picks():
    data = load("real_flight_cx271.json")
    flights = adb.validate_flights_payload(data)
    p = adb._pick_flight(flights, "2025-01-11", "hkg", "EHAM")
    assert p.kind == "found" and p.flight["callSign"] == "CPA271"
    assert adb._pick_flight(flights, "2025-01-12").kind == "not_found"


# --- aircraft --------------------------------------------------------------
TODAY = dt.date(2026, 10, 3)


def test_map_real_record():
    m = adb.map_aircraft(load("real_aircraft_9vsmg.json"), TODAY)
    assert m == {"ac_type": "A359", "ac_model": "Airbus A350-900", "ac_age": 9.9, "ac_built": "2016-11-17"}


@pytest.mark.parametrize("rec,expected", [
    ({"icaoCode": "A359", "modelCode": "350-941"}, {"ac_type": "A359"}),
    ({"modelCode": "350-941"}, {"ac_type": "350-941"}),
    ({"typeCode": "B739"}, {"ac_type": None}),                         # field does not exist upstream
    ({"typeName": "Airbus A350-900", "model": "A359"}, {"ac_model": "Airbus A350-900"}),
    ({"model": "A359"}, {"ac_model": "A359"}),
    ({"rolloutDate": "2016-11-17", "firstFlightDate": "2012-01-01"}, {"ac_built": "2016-11-17", "ac_age": 9.9}),
    ({"firstFlightDate": "2016-11-17"}, {"ac_built": "2016-11-17"}),
    ({"registrationDate": "2018"}, {"ac_age": 8.8, "ac_built": "2018"}),
    ({"rolloutDate": "2010-06"}, {"ac_age": 16.3}),
    ({"ageYears": 7.44}, {"ac_age": 7.4, "ac_built": None}),           # only when no date works
    ({"rolloutDate": "bogus", "ageYears": 3}, {"ac_age": 3.0}),
    ({"rolloutDate": "2999-01-01", "ageYears": 3}, {"ac_age": 3.0}),   # future date skipped
    ({"ageYears": -3}, {"ac_age": None}),
    ({"ageYears": 120}, {"ac_age": None}),
    ({"ageYears": True}, {"ac_age": None}),
    ({"rolloutDate": "1900-01-01"}, {"ac_age": None}),                 # > 80
    ({}, {"ac_type": None, "ac_model": None, "ac_age": None, "ac_built": None}),
    (None, {"ac_type": None, "ac_age": None}),
    ({"icaoCode": 5, "model": ["x"]}, {"ac_type": None, "ac_model": None}),
])
def test_map_aircraft_table(rec, expected):
    m = adb.map_aircraft(rec, TODAY)
    for k, v in expected.items():
        assert m[k] == v, (rec, k)


def test_map_fixture_variants():
    assert adb.map_aircraft(load("aircraft_bad_age.json"), TODAY)["ac_age"] is None
    assert adb.map_aircraft(load("aircraft_ancient.json"), TODAY)["ac_age"] is None
    assert adb.map_aircraft(load("aircraft_dates_only.json"), TODAY)["ac_age"] == 8.8
    assert adb.map_aircraft(load("aircraft_empty.json"), TODAY)["ac_type"] is None


def reg(code="9V-SMG", **kw):
    async def go():
        try:
            return await adb.aircraft_by_reg(code, **kw)
        finally:
            await adb.aclose()
    return run(go())


def test_aircraft_ok_cached_and_path(env):
    up = env.use(Upstream(resp(200, "real_aircraft_9vsmg.json")))
    assert reg()["icaoCode"] == "A359"
    assert reg()["icaoCode"] == "A359" and up.calls == 1
    assert up.requests[0].url.path == "/aircrafts/reg/9V-SMG"


def test_aircraft_204_negative_cached(env):
    up = env.use(Upstream((204, None)))
    assert reg() is None and reg() is None and up.calls == 1


@pytest.mark.parametrize("item", [resp(200, "junk_null.json"), resp(200, "junk_message.json"),
                                  resp(200, "junk_items.json"), (500, {"message": "x"}),
                                  httpx.ReadTimeout("t")])
def test_aircraft_failures_are_none_and_not_cached(env, item):
    up = env.use(Upstream(item))
    assert reg() is None
    n = up.calls
    assert reg() is None and up.calls > n


def test_aircraft_list_body_is_not_accepted(env):
    env.use(Upstream((200, [{"reg": "N1", "icaoCode": "B738"}])))
    assert reg("N1") is None


def test_aircraft_bad_reg_makes_no_call(env):
    up = env.use(Upstream((200, {"reg": "x"})))
    assert reg("../x") is None and reg("") is None and up.calls == 0
