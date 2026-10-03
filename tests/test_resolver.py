"""Resolver: status mapping, hints, ambiguity, manual rows, caching, refresh,
concurrency and deadline. Offline."""

import asyncio

import httpx
import pytest
from fixtures.loader import Upstream, env, future_date, load, resp  # noqa: F401

from app import aerodatabox as adb
from app import config, resolver, validation

run = asyncio.run
D = future_date(10)


def rows(*specs):
    """spec: (flight_no, from, to) or (flight_no, from, to, date)."""
    out = []
    for i, s in enumerate(specs):
        fn, a, b, *d = s
        out.append(
            {"direction": "out", "seq": i, "flight_no": fn, "date": d[0] if d else D, "from": a, "to": b}
        )
    return out


def go(*specs, **kw):
    async def inner():
        try:
            return await resolver.resolve_rows(rows(*specs), **kw)
        finally:
            await adb.aclose()

    return run(inner())


def one(fn="DL1200", a=None, b=None, date=D):
    return go((fn, a, b, date))[0]


class Router:
    """Path-routed async upstream. flights: {flight_no: Response-factory}."""

    def __init__(self, flights=None, aircraft=None, delay=0.0):
        self.flights = flights or {}
        self.aircraft = aircraft
        self.delay = delay
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0

    async def __call__(self, request):
        self.calls.append(request.url.path)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            parts = request.url.path.split("/")
            if "aircrafts" in parts:
                return self.aircraft() if self.aircraft else httpx.Response(204)
            fn = parts[3]
            make = self.flights.get(fn)
            return make() if make else httpx.Response(204)
        finally:
            self.inflight -= 1

    @property
    def transport(self):
        return httpx.MockTransport(self)

    @property
    def calls_n(self):
        return len(self.calls)


def single():
    return resp(200, "flights_single.json", D)


def aircraft_ok():
    return resp(200, "real_aircraft_9vsmg.json")


# --- ok --------------------------------------------------------------------
def test_ok_flight_with_aircraft(env):
    env.use(Router({"DL1200": single}, aircraft_ok))
    r = one()
    assert r["status"] == "ok" and r["resolved"] == 1 and r["manual"] == 0
    assert (r["dep_icao"], r["dep_iata"], r["arr_icao"], r["arr_iata"]) == ("KATL", "ATL", "KDEN", "DEN")
    assert r["dep_name"] == "Atlanta Hartsfield-Jackson" and r["dep_lat"] == 33.64  # local table wins
    assert r["dep_local"] == f"{D} 08:15-04:00" and r["arr_utc"] == f"{D} 15:05Z"
    assert r["callsign"] == "DAL1200" and r["reg"] == "N123DN"
    assert r["ac_type"] == "A359" and r["ac_model"] == "Airbus A350-900"
    assert r["ac_age"] is not None and r["ac_built"] == "2016-11-17"
    assert r["flight_status"] == "Expected"
    assert r["candidates"] == [] and r["unverified"] is False
    assert r["message"] == "DL1200: ATL to DEN."
    assert r["date_local"] == D and r["direction"] == "out" and r["seq"] == 0


def test_flight_level_model_survives_when_record_has_only_short_code(env):
    env.use(
        Router({"DL1200": single}, lambda: httpx.Response(200, json={"icaoCode": "B739", "model": "B739"}))
    )
    r = one()
    assert r["ac_model"] == "Boeing 737-900" and r["ac_type"] == "B739"


def test_ok_without_registration_makes_no_aircraft_call(env):
    up = env.use(Router({"DL1200": lambda: resp(200, "flights_single_no_reg.json", D)}, aircraft_ok))
    r = one()
    assert r["status"] == "ok" and r["reg"] is None and r["ac_type"] is None
    assert up.calls_n == 1


def test_aircraft_failure_does_not_fail_the_flight(env):
    env.use(Router({"DL1200": single}, lambda: httpx.Response(500, json={"message": "x"})))
    r = one()
    assert r["status"] == "ok" and r["reg"] == "N123DN" and r["ac_type"] is None


def test_extra_keys_from_real_flight_shape(env):
    flight = load("real_flight_cx271.json")[0]
    leg = resolver._blank_leg("out", 0, "CX271", "2025-01-11")
    assert resolver._apply_flight(leg, flight, "CX271") is True
    assert (leg["dep_icao"], leg["arr_icao"]) == ("VHHH", "EHAM")  # not in local table: upstream values
    assert leg["dep_lat"] == 22.3089 and leg["arr_name"] == "Amsterdam Schiphol"
    assert leg["callsign"] == "CPA271" and leg["reg"] == "B-LXD" and leg["ac_model"] == "Airbus A350"
    assert leg["flight_status"] == "Arrived"
    assert (
        leg["dep_revised_utc"] == "2025-01-11 15:20Z" and leg["arr_revised_local"] == "2025-01-12 06:09+01:00"
    )
    assert leg["dep_city"] == "Hong Kong" and leg["arr_city"] == "Amsterdam"
    assert leg["resolved"] == 1


# --- callsign --------------------------------------------------------------
@pytest.mark.parametrize(
    "f,no,expected",
    [
        ({"callSign": "dal1200"}, "DL1200", "DAL1200"),
        ({"airline": {"icao": "DAL"}}, "DL1200", "DAL1200"),
        ({"airline": {"icao": "DAL"}}, "DL0042", "DAL0042"),  # digits kept as typed
        ({"airline": {"icao": "DAL"}}, "DL1200A", "DAL1200"),
        ({"callSign": "bad callsign!", "airline": {"icao": "DAL"}}, "DL12", "DAL12"),
        ({}, "DL1200", None),  # never the raw flight number
        ({"airline": {"icao": ""}}, "DL1200", None),
        ({"airline": {"icao": "DL"}}, "DL1200", None),
        ({"callSign": None, "airline": None}, "DL1200", None),
    ],
)
def test_derive_callsign(f, no, expected):
    assert resolver.derive_callsign(f, no) == expected


def test_callsign_derived_end_to_end(env):
    env.use(Router({"DL1200": lambda: resp(200, "flights_single_no_callsign.json", D)}))
    assert one()["callsign"] == "DAL1200"


# --- not found, hints, ambiguity ------------------------------------------
def test_204_not_found_message(env):
    env.use(Router())
    r = one()
    assert r["status"] == "not_found" and r["resolved"] == 0 and r["manual"] == 0
    assert r["message"].startswith("DL1200 not found on ")
    assert "add the airports yourself" in r["message"]


def test_wrong_date_is_not_found_and_mentions_neighbour_day(env):
    env.use(Router({"DL1200": lambda: resp(200, "flights_wrong_date.json", D)}))
    r = one()
    assert r["status"] == "not_found" and r["resolved"] == 0 and r["dep_icao"] is None
    assert "but is on" in r["message"] and r["candidates"] == []


def test_route_mismatch_with_hints_fills_manual_but_stays_not_found(env):
    env.use(Router({"DL1200": single}))
    r = one(a="JAX", b="LHR")
    assert r["status"] == "not_found" and r["resolved"] == 0 and r["manual"] == 1
    assert (r["dep_icao"], r["arr_icao"]) == ("KJAX", "EGLL")
    assert "not on that route" in r["message"]
    assert [c["from"] for c in r["candidates"]] == ["KATL"]


def test_single_candidate_hint_match_is_ok_in_either_code_style(env):
    env.use(Router({"DL1200": single}))
    assert one(a="atl", b="KDEN")["status"] == "ok"


def multileg():
    return resp(200, "flights_multileg.json", D)


def test_ambiguous_returns_sorted_candidates_and_never_picks(env):
    env.use(Router({"AA300": multileg}))
    r = one("AA300")
    assert r["status"] == "ambiguous" and r["resolved"] == 0 and r["dep_icao"] is None
    assert [(c["from"], c["to"], c["from_iata"], c["to_iata"]) for c in r["candidates"]] == [
        ("KJAX", "KATL", "JAX", "ATL"),
        ("KATL", "KDEN", "ATL", "DEN"),
    ]  # earlier departure first; codeshare deduped
    assert set(r["candidates"][0]) == {"from", "from_iata", "to", "to_iata", "dep_local", "arr_local"}
    assert "2 legs" in r["message"] and "Pick" in r["message"]


@pytest.mark.parametrize("a,b,dep", [("JAX", None, "KJAX"), (None, "DEN", "KATL"), ("KATL", "KDEN", "KATL")])
def test_ambiguous_resolved_by_hint(env, a, b, dep):
    env.use(Router({"AA300": multileg}))
    r = one("AA300", a, b)
    assert r["status"] == "ok" and r["dep_icao"] == dep


def test_incomplete_flight_is_not_resolved(env):
    bad = [
        {
            "number": "DL 1200",
            "departure": {
                "airport": {"icao": "KATL"},
                "scheduledTime": {"local": f"{D} 08:00-04:00", "utc": f"{D} 12:00Z"},
            },
            "arrival": {"scheduledTime": {}},
        }
    ]
    env.use(Router({"DL1200": lambda: httpx.Response(200, json=bad)}))
    r = one()
    assert r["status"] == "not_found" and r["resolved"] == 0 and r["arr_icao"] is None


# --- dates -----------------------------------------------------------------
@pytest.mark.parametrize(
    "date,status",
    [
        (future_date(-30), "out_of_window"),
        (future_date(-3), "out_of_window"),
        (future_date(400), "out_of_window"),
        ("2026-13-45", "invalid"),
        ("garbage", "invalid"),
    ],
)
def test_date_window(env, date, status):
    up = env.use(Router({"DL1200": single}))
    r = one(date=date)
    assert r["status"] == status and r["resolved"] == 0 and r["message"]
    assert up.calls_n == 0


def test_window_knobs_are_read(env, monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_WINDOW_FUTURE_DAYS", 5, raising=False)
    env.use(Router({"DL1200": single}))
    assert one(date=D)["status"] == "out_of_window"


def test_missing_date_defaults_to_today(env):
    from fixtures.loader import today

    env.use(Router())
    r = go(("DL1200", None, None, ""))[0]
    assert r["date_local"] == today().isoformat() and r["status"] == "not_found"


# --- manual rows -----------------------------------------------------------
def test_manual_ok(env):
    up = env.use(Router())
    r = one("", "JAX", "kden")
    assert r["status"] == "manual_ok" and r["manual"] == 1 and r["resolved"] == 0
    assert (r["dep_icao"], r["arr_icao"], r["flight_no"]) == ("KJAX", "KDEN", None)
    assert r["message"] == "Using JAX to DEN."
    assert up.calls_n == 0


def test_manual_unknown_airport(env):
    r = one("", "JAX", "XYZ")
    assert r["status"] == "airport_unknown" and r["message"] == "Unknown airport XYZ."
    assert one("", "QQQ", "JAX")["message"] == "Unknown airport QQQ."


def test_manual_missing_side_is_invalid(env):
    assert one("", "JAX", None)["status"] == "invalid"
    assert one("", None, None)["status"] == "invalid"


# --- upstream failures -----------------------------------------------------
@pytest.mark.parametrize(
    "item,status,needle",
    [
        ((401, {"message": "bad"}), "upstream_unavailable", "person who runs this board"),
        ((403, {"message": "bad"}), "upstream_unavailable", "person who runs this board"),
        ((429, {"message": "slow"}), "quota", "rate limited"),
        ((500, {"message": "x"}), "upstream_unavailable", "not responding"),
        (httpx.ReadTimeout("t"), "upstream_unavailable", "not responding"),
        (httpx.ConnectError("c"), "upstream_unavailable", "not responding"),
        ((200, {"message": "junk"}), "upstream_unavailable", "not responding"),
    ],
)
def test_upstream_failures(env, item, status, needle):
    env.use(Upstream(item))
    r = one(a="JAX", b="DEN")
    assert r["status"] == status and needle in r["message"]
    assert r["resolved"] == 0
    # hints still fill the manual route so the caller may keep it as unverified
    assert r["manual"] == 1 and (r["dep_icao"], r["arr_icao"]) == ("KJAX", "KDEN")
    assert "secret-test-key" not in r["message"]


def test_failure_without_hints_has_no_route(env):
    env.use(Upstream((500, {"message": "x"})))
    r = one()
    assert r["manual"] == 0 and r["dep_icao"] is None


def test_missing_key_message_points_to_operator(env, monkeypatch):
    monkeypatch.setattr(config, "AERODATABOX_KEY", "")
    r = one()
    assert r["status"] == "upstream_unavailable" and "person who runs" in r["message"]


def test_messages_have_no_blame_or_jargon():
    for m in (resolver.MSG_BAD_KEY, resolver.MSG_QUOTA, resolver.MSG_DOWN, resolver.MSG_DEADLINE):
        low = m.lower()
        assert not any(
            w in low for w in ("error", "api", "http", "exception", "invalid", "you must", "your fault")
        )
        assert m[0].isupper() and m.endswith(".")


# --- caching, refresh ------------------------------------------------------
def test_second_resolution_is_free(env):
    up = env.use(Router({"DL1200": single}, aircraft_ok))
    one()
    n = up.calls_n
    assert one()["status"] == "ok" and up.calls_n == n


def test_negative_result_is_cached_briefly(env):
    up = env.use(Router())
    one()
    one()
    assert up.calls_n == 1


def stored(r):
    return {k: v for k, v in r.items() if k not in ("status", "message", "candidates", "unverified")}


def test_refresh_leg_bypasses_cache_and_returns_only_changes(env):
    first = Router({"DL1200": lambda: resp(200, "flights_single_no_reg.json", D)})
    env.use(first)
    leg = stored(one())
    assert leg["reg"] is None
    second = Router({"DL1200": single}, aircraft_ok)
    adb.set_transport(second.transport)
    changed = run(resolver.refresh_leg(leg))
    assert second.calls_n >= 1  # not served from cache
    assert changed["reg"] == "N123DN" and changed["ac_type"] == "A359" and "ac_age" in changed
    assert "flight_no" not in changed and "date_local" not in changed and "dep_icao" not in changed
    # a second refresh with nothing new reports no change
    leg.update(changed)
    assert run(resolver.refresh_leg(leg)) is None


def test_refresh_leg_never_blanks_data_and_ignores_failures(env):
    env.use(Router({"DL1200": single}, aircraft_ok))
    leg = stored(one())
    adb.set_transport(Upstream((500, {"message": "x"})).transport)
    assert run(resolver.refresh_leg(leg)) is None
    adb.set_transport(Router({"DL1200": lambda: resp(200, "flights_single_no_reg.json", D)}).transport)
    assert run(resolver.refresh_leg(leg)) is None  # reg missing now: keep what we have


def test_refresh_leg_manual_or_dateless_is_none(env):
    assert run(resolver.refresh_leg({"flight_no": None, "date_local": D})) is None
    assert run(resolver.refresh_leg({"flight_no": "DL1200", "date_local": None})) is None


# --- concurrency and deadline ---------------------------------------------
def test_results_keep_order_and_concurrency_is_bounded(env, monkeypatch):
    monkeypatch.setattr(config, "RESOLVE_CONCURRENCY", 3, raising=False)
    nums = ["DL1200", "DL1201", "DL1202", "DL1203", "DL1204", "DL1205", "DL1206", "DL1207"]
    up = env.use(Router({n: single for n in nums}, delay=0.02))
    out = go(*[(n, None, None) for n in nums])
    assert [r["flight_no"] for r in out] == nums
    assert [r["seq"] for r in out] == list(range(8))
    assert all(r["status"] == "ok" for r in out)
    assert 1 < up.max_inflight <= 3


def test_deadline_turns_slow_rows_into_friendly_status(env):
    env.use(Router({"DL1200": single}, delay=5))
    import time

    t0 = time.monotonic()
    out = go(("DL1200", "JAX", "DEN"), ("", "JAX", "DEN"), ("DL1201", None, None), deadline=0.2)
    assert time.monotonic() - t0 < 2
    assert [r["status"] for r in out] == ["upstream_unavailable", "manual_ok", "upstream_unavailable"]
    assert out[0]["message"] == resolver.MSG_DEADLINE and out[0]["flight_no"] == "DL1200"
    assert out[0]["manual"] == 1  # hints still fill the route


def test_one_row_crashing_does_not_sink_the_rest(env, monkeypatch):
    env.use(Router({"DL1200": single}))
    real = resolver._resolve_one

    async def flaky(row, **kw):
        if row["flight_no"] == "DL1201":
            raise RuntimeError("boom")
        return await real(row, **kw)

    monkeypatch.setattr(resolver, "_resolve_one", flaky)
    out = go(("DL1200", None, None), ("DL1201", None, None))
    assert [r["status"] for r in out] == ["ok", "upstream_unavailable"]


def test_empty_input():
    assert run(resolver.resolve_rows([])) == []


def test_deadline_default_comes_from_config(env, monkeypatch):
    monkeypatch.setattr(config, "RESOLVE_DEADLINE", 0.1, raising=False)
    env.use(Router({"DL1200": single}, delay=3))
    out = go(("DL1200", None, None))
    assert out[0]["message"] == resolver.MSG_DEADLINE


# --- re-exports and legacy -------------------------------------------------
def test_reexports():
    assert resolver.normalize_flight_no is validation.normalize_flight_no
    assert resolver.parse_adb_dt is adb.parse_adb_dt
    assert resolver.parse_adb_dt("2026-06-04 08:00-04:00").hour == 12
    assert resolver.parse_adb_dt("junk") is None and resolver.parse_adb_dt(None) is None


def test_legacy_resolve_leg_returns_db_columns_only(env):
    env.use(Router({"DL1200": single}))
    leg = run(resolver.resolve_leg("out", 0, "dl 1200", D))
    assert leg["resolved"] == 1 and "status" not in leg and "message" not in leg
