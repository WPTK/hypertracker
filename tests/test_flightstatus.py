"""AeroDataBox flight status: the fallback for legs airplanes.live cannot see."""

import asyncio
import datetime as dt

import pytest

from app import aerodatabox as adb
from app import flightstatus as fs

NOW = dt.datetime(2026, 10, 3, 19, 40, tzinfo=dt.UTC)


def flight(status="EnRoute", dep_runway="2026-10-03 18:05Z", arr_runway=None, dep="YYZ", arr="NRT"):
    f = {
        "status": status,
        "departure": {
            "airport": {"iata": dep},
            "scheduledTime": {"utc": "2026-10-03 17:40Z"},
            "revisedTime": {"utc": "2026-10-03 17:44Z"},
        },
        "arrival": {
            "airport": {"iata": arr},
            "scheduledTime": {"utc": "2026-10-04 07:10Z"},
            "revisedTime": {"utc": "2026-10-04 07:20Z"},
        },
    }
    if dep_runway:
        f["departure"]["runwayTime"] = {"utc": dep_runway}
    if arr_runway:
        f["arrival"]["runwayTime"] = {"utc": arr_runway}
    return f


LEG = {"flight_no": "AC9", "date_local": "2026-10-03", "from_iata": "YYZ", "to_iata": "NRT"}


@pytest.fixture(autouse=True)
def clean():
    fs.reset()
    yield
    fs.reset()


def test_enroute_is_airborne_with_actual_departure_and_revised_arrival():
    s = fs.summarise(flight(), NOW)
    assert s == {"state": "airborne", "dep_utc_est": "2026-10-03 18:05Z", "arr_utc_est": "2026-10-04 07:20Z"}


def test_a_future_runway_time_does_not_mean_airborne():
    s = fs.summarise(flight(status="Expected", dep_runway="2026-10-03 21:00Z"), NOW)
    assert s["state"] is None


def test_runway_time_in_the_past_means_airborne_even_without_a_status():
    assert fs.summarise(flight(status="Unknown"), NOW)["state"] == "airborne"


@pytest.mark.parametrize(
    "kw,state",
    [
        ({"status": "Arrived"}, "landed"),
        ({"status": "EnRoute", "arr_runway": "2026-10-04 07:00Z"}, "landed"),
        ({"status": "Canceled", "dep_runway": None}, "cancelled"),
        ({"status": "CanceledUncertain", "dep_runway": None}, "cancelled"),
        ({"status": "Delayed", "dep_runway": None}, "delayed"),
        ({"status": "Expected", "dep_runway": None}, None),
        ({"status": "Approaching"}, "airborne"),
    ],
)
def test_status_mapping(kw, state):
    assert fs.summarise(flight(**kw), NOW)["state"] == state


def test_pick_prefers_the_matching_route_and_takes_a_lone_result():
    a, b = flight(dep="YYZ", arr="YVR"), flight(dep="YVR", arr="NRT")
    assert fs.pick([a, b], {"from_iata": "YVR", "to_iata": "NRT"}) is b
    assert fs.pick([a], {"from_iata": "XXX", "to_iata": "YYY"}) is a
    assert fs.pick([a, b], {"from_iata": "XXX", "to_iata": "YYY"}) is None


def test_polling_interval_slows_en_route_and_speeds_up_near_arrival():
    far = fs._interval("airborne", "2026-10-04 07:10Z", NOW)
    near = fs._interval("airborne", "2026-10-03 20:00Z", NOW)
    assert far == fs.EN_ROUTE_EVERY and near == fs.NEAR_ARRIVAL_EVERY
    assert fs._interval(None, None, NOW) == fs.PRE_DEPARTURE_EVERY


def run_refresh(monkeypatch, result):
    async def fake_fetch(no, date, **kw):
        return result

    monkeypatch.setattr(fs, "_due", lambda ts: [dict(LEG)])
    monkeypatch.setattr(adb, "fetch_flights", fake_fetch)
    return asyncio.run(fs.refresh_once())


def test_refresh_stores_status_and_schedules_the_next_fetch(monkeypatch):
    assert run_refresh(monkeypatch, adb.FlightsResult(kind=adb.KIND_OK, flights=[flight()])) == 1
    assert fs.get(LEG)["state"] == "airborne"
    assert fs._next_at[fs._key(LEG)] > NOW.timestamp()


def test_a_failed_fetch_keeps_the_last_known_status(monkeypatch):
    run_refresh(monkeypatch, adb.FlightsResult(kind=adb.KIND_OK, flights=[flight()]))
    run_refresh(monkeypatch, adb.FlightsResult(kind=adb.KIND_QUOTA))
    assert fs.get(LEG)["state"] == "airborne"
