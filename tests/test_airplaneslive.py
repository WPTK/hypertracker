"""airplanes.live poller: classification, failure handling, spacing, expiry,
pruning. Offline, with an injected clock and a fake sleep."""

import asyncio
import logging

import httpx
import pytest
from fixtures.loader import FIX, load

from app import airplaneslive as al

run = asyncio.run


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    al.reset()
    t = [1_800_000_000.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        t[0] += s

    monkeypatch.setattr(al, "_clock", lambda: t[0])

    class C:
        now = t
        sleeps = slept
        fake_sleep = staticmethod(sleep)

    yield C
    al.reset()


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def poll(handler, callsigns, clock):
    async def go():
        async with client_for(handler) as c:
            await al.poll_once(c, callsigns, sleep=clock.fake_sleep)

    run(go())


def ok_body(name):
    return lambda request: httpx.Response(200, content=(FIX / name).read_bytes())


# --- classification --------------------------------------------------------
@pytest.mark.parametrize(
    "ac,expected",
    [
        ({"alt_baro": "ground"}, "on_ground"),
        ({"alt_baro": "GROUND"}, "on_ground"),
        ({"alt_baro": 31000}, "airborne"),
        ({"alt_baro": 25, "gs": 5}, "airborne"),  # numeric > 0 wins over gs
        ({"alt_baro": 0}, "on_ground"),
        ({"alt_baro": -50}, "on_ground"),
        ({"gs": 300}, "airborne"),  # no altitude: ground speed fallback
        ({"gs": 40.5}, "airborne"),
        ({"gs": 40}, None),
        ({"gs": 5}, None),
        ({"alt_baro": True}, None),
        ({"alt_baro": "weird"}, None),
        ({}, None),
        (None, None),
        ("x", None),
    ],
)
def test_classify_aircraft(ac, expected):
    assert al.classify_aircraft(ac) == expected


def test_classify_response_prefers_airborne():
    assert al.classify_response(load("live_mixed.json")["ac"]) == "airborne"
    assert al.classify_response([{"alt_baro": "ground"}]) == "on_ground"
    assert al.classify_response([]) is None
    assert al.classify_response([{}, {"alt_baro": "ground"}]) == "on_ground"


def test_classify_ignores_other_callsigns_and_strips_padding():
    ac = [{"flight": "UAL9   ", "alt_baro": 30000}, {"flight": "DAL1200 ", "alt_baro": "ground"}]
    assert al.classify_response(ac, "DAL1200") == "on_ground"


# --- fixtures through the poller ------------------------------------------
@pytest.mark.parametrize(
    "name,callsign,expected",
    [
        ("real_live_rch4525.json", "RCH4525", "airborne"),
        ("synthetic_live_ground.json", "UAL123", "on_ground"),
        ("live_airborne.json", "DAL1200", "airborne"),
        ("live_ground_string.json", "DAL1200", "on_ground"),
        ("live_gs_only_fast.json", "DAL1200", "airborne"),
        ("live_gs_only_slow.json", "DAL1200", None),
        ("live_mixed.json", "DAL1200", "airborne"),
        ("live_empty.json", "DAL1200", None),
    ],
)
def test_poll_fixture_states(clock, name, callsign, expected):
    poll(ok_body(name), [callsign], clock)
    assert al.get_state(callsign) == expected
    assert al.get_state(callsign.lower()) == expected


def test_request_shape(clock):
    seen = []

    def h(request):
        seen.append(request)
        return httpx.Response(200, json={"ac": []})

    poll(h, ["DAL1200"], clock)
    assert seen[0].url.path.endswith("/callsign/DAL1200")
    assert "hypertracker" in seen[0].headers["user-agent"]


# --- failures keep previous state -----------------------------------------
@pytest.mark.parametrize(
    "failure",
    [
        lambda r: httpx.Response(429, json={"message": "slow down"}),
        lambda r: httpx.Response(403),
        lambda r: httpx.Response(503, text="down"),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: httpx.Response(200, json={"ac": "nope"}),
        lambda r: httpx.Response(200, json=[1, 2]),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("c")),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")),
    ],
)
def test_failure_keeps_previous_state(clock, failure):
    poll(ok_body("live_airborne.json"), ["DAL1200"], clock)
    assert al.get_state("DAL1200") == "airborne"
    stamp = al.snapshot_updated_at()
    clock.now[0] += 30
    poll(failure, ["DAL1200"], clock)
    assert al.get_state("DAL1200") == "airborne"  # kept, never flipped to a "not airborne"
    assert al.snapshot_updated_at() == stamp  # no successful refresh happened


def test_failure_with_no_previous_state_stays_unknown(clock):
    poll(lambda r: httpx.Response(500), ["DAL1200"], clock)
    assert al.get_state("DAL1200") is None and al.snapshot_updated_at() is None


def test_transition_airborne_to_ground_to_not_seen(clock):
    poll(ok_body("live_airborne.json"), ["DAL1200"], clock)
    poll(ok_body("live_ground_string.json"), ["DAL1200"], clock)
    assert al.get_state("DAL1200") == "on_ground"
    poll(ok_body("live_empty.json"), ["DAL1200"], clock)
    assert al.get_state("DAL1200") is None


def test_falls_back_to_registration_when_callsign_finds_nothing(clock):
    """Regression: AC9's schedule callsign (ACA9) matched no aircraft, so a flight in the
    air stayed 'Scheduled'. The tail number finds it."""
    al.set_regs({"ACA9": "C-FNND"})
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "/callsign/" in request.url.path:
            return httpx.Response(200, json={"ac": []})
        return httpx.Response(200, json={"ac": [{"flight": "ACA0009 ", "alt_baro": 36000}]})

    poll(handler, ["ACA9"], clock)
    assert seen[0].endswith("/callsign/ACA9") and seen[1].endswith("/reg/C-FNND")
    assert al.get_state("ACA9") == "airborne"


def test_registration_is_not_queried_when_callsign_matches(clock):
    al.set_regs({"DAL1200": "N123DL"})
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=(FIX / "live_airborne.json").read_bytes())

    poll(handler, ["DAL1200"], clock)
    assert len(seen) == 1 and al.get_state("DAL1200") == "airborne"


def test_set_regs_drops_junk():
    al.set_regs({"ACA9": "../x", "bad cs": "C-FNND", "DAL1": "n1dl"})
    assert al._regs == {"DAL1": "N1DL"}


def test_failures_log_at_warning_rate_limited(clock, caplog):
    with caplog.at_level(logging.WARNING, logger="hypertracker.airplaneslive"):
        for _ in range(3):
            poll(lambda r: httpx.Response(500), ["DAL1200"], clock)
        assert len(caplog.records) == 1
        clock.now[0] += 61
        poll(lambda r: httpx.Response(500), ["DAL1200"], clock)
    assert len(caplog.records) == 2 and all(r.levelno == logging.WARNING for r in caplog.records)


def test_429_abandons_the_cycle(clock):
    n = []

    def h(r):
        n.append(1)
        return httpx.Response(429)

    poll(h, ["AAA1", "BBB2", "CCC3"], clock)
    assert len(n) == 1


def test_network_error_on_one_callsign_does_not_stop_the_rest(clock):
    def h(r):
        if r.url.path.endswith("BAD1"):
            raise httpx.ConnectError("c")
        return httpx.Response(200, content=(FIX / "live_airborne.json").read_bytes())

    poll(h, ["BAD1", "DAL1200"], clock)
    assert al.get_state("DAL1200") == "airborne" and al.get_state("BAD1") is None


# --- spacing ---------------------------------------------------------------
def test_requests_are_spaced_at_least_one_second(clock):
    times = []

    def h(r):
        times.append(clock.now[0])
        return httpx.Response(200, json={"ac": []})

    poll(h, ["AAA1", "BBB2", "CCC3", "DDD4"], clock)
    assert len(times) == 4
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:], strict=False))
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_spacing_accounts_for_slow_requests(clock):
    times = []

    def h(r):
        times.append(clock.now[0])
        clock.now[0] += 0.4  # the request itself took 0.4s
        return httpx.Response(200, json={"ac": []})

    poll(h, ["AAA1", "BBB2"], clock)
    assert times[1] - times[0] >= 1.0
    assert clock.sleeps == [pytest.approx(0.6)]


# --- expiry, pruning, snapshot --------------------------------------------
def test_state_expires_after_five_minutes_without_refresh(clock):
    poll(ok_body("live_airborne.json"), ["DAL1200"], clock)
    clock.now[0] += 299
    assert al.get_state("DAL1200") == "airborne"
    clock.now[0] += 2
    assert al.get_state("DAL1200") is None
    assert "DAL1200" not in al._states


def test_snapshot_updated_at(clock):
    assert al.snapshot_updated_at() is None
    poll(ok_body("live_empty.json"), ["DAL1200"], clock)
    assert al.snapshot_updated_at() == int(clock.now[0])


def test_get_state_is_pure_and_tolerates_junk(clock):
    for v in (None, "", "  ", "bad callsign", "A", "TOOLONGCALLSIGN", 12, "dal/../1"):
        assert al.get_state(v) is None


def test_run_poller_prunes_validates_and_cancels_cleanly(clock):
    seen = []
    cycles = [["DAL1200", "dal1200", "bad callsign!", "../x", "UAL9"], ["UAL9"]]
    interval_sleeps = []

    async def sleep(s):
        if s == 60.0:
            interval_sleeps.append(s)
            if len(interval_sleeps) == 2:
                raise asyncio.CancelledError
        await clock.fake_sleep(s)

    def handler(r):
        seen.append(r.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, content=(FIX / "live_airborne.json").read_bytes())

    def supplier():
        return cycles.pop(0)

    async def go():
        async with client_for(handler) as c:
            with pytest.raises(asyncio.CancelledError):
                await al.run_poller(supplier, 60.0, client=c, sleep=sleep)
            assert not c.is_closed  # an injected client is not ours to close

    run(go())
    assert seen == ["DAL1200", "UAL9", "UAL9"]  # deduped, invalid callsigns never requested
    assert set(al._states) == {"UAL9"}  # DAL1200 pruned once nobody asked for it


def test_run_poller_survives_a_broken_supplier(clock):
    calls = []

    async def sleep(s):
        calls.append(s)
        if len(calls) >= 3:
            raise asyncio.CancelledError
        await clock.fake_sleep(s)

    def bad():
        raise RuntimeError("db down")

    async def go():
        async with client_for(lambda r: httpx.Response(200, json={"ac": []})) as c:
            with pytest.raises(asyncio.CancelledError):
                await al.run_poller(bad, 60.0, client=c, sleep=sleep)

    run(go())
    assert len(calls) == 3


def test_run_poller_closes_its_own_client_on_cancel(clock, monkeypatch):
    created = []
    real = httpx.AsyncClient

    def make(*a, **k):
        c = real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ac": []})))
        created.append(c)
        return c

    monkeypatch.setattr(al.httpx, "AsyncClient", make)

    async def sleep(s):
        raise asyncio.CancelledError

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await al.run_poller(lambda: [], 60.0, sleep=sleep)

    run(go())
    assert created and created[0].is_closed
