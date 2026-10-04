"""legStatus is the one place the board decides what to claim about a flight. Run the
real ES module through Node (no browser needed)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "format.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

DEP, ARR = "2026-10-10 12:00Z", "2026-10-10 16:00Z"
T = {
    "before": "2026-10-10T11:00:00Z",
    "during": "2026-10-10T13:00:00Z",
    "after": "2026-10-10T17:00:00Z",
}


def status(leg: dict, when: str) -> dict:
    script = (
        f"import {{ legStatus }} from {json.dumps(MODULE.as_uri())};"
        f"console.log(JSON.stringify(legStatus({json.dumps(leg)}, Date.parse({json.dumps(when)}))));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def leg(**kw):
    base = {"dep_utc": DEP, "arr_utc": ARR, "live_state": None, "unverified": False, "manual": False}
    return {**base, **kw}


def test_live_airborne_wins_while_flying():
    assert status(leg(live_state="airborne"), T["during"])["label"] == "Airborne"


def test_scheduled_before_departure():
    assert status(leg(), T["before"])["label"] == "Scheduled"


def test_past_departure_without_live_data_does_not_claim_airborne():
    assert status(leg(), T["during"])["label"] == "Past departure"


def test_on_ground_and_landed():
    assert status(leg(live_state="on_ground"), T["during"])["label"] == "On ground"
    assert status(leg(), T["after"])["label"] == "Landed"


def test_unverified_beats_schedule_guess():
    assert status(leg(unverified=True), T["during"])["label"] == "Unverified"


def test_aerodatabox_status_covers_legs_the_live_feed_cannot_see():
    assert status(leg(flight_status="airborne"), T["during"])["label"] == "Airborne"
    assert status(leg(flight_status="landed"), T["during"])["label"] == "Landed"
    assert status(leg(flight_status="cancelled"), T["before"]) == {"key": "cancelled", "label": "Cancelled"}


def test_delayed_until_the_revised_departure_then_past_departure():
    delayed = leg(flight_status="delayed", dep_utc_est="2026-10-10 14:00Z", arr_utc_est="2026-10-10 18:00Z")
    assert status(delayed, T["during"])["label"] == "Delayed"
    assert status(delayed, "2026-10-10T14:30:00Z")["label"] == "Past departure"


def test_landed_follows_the_revised_arrival_not_the_schedule():
    late = leg(arr_utc_est="2026-10-10 18:00Z")
    assert status(late, T["after"])["label"] != "Landed"
    assert status(late, "2026-10-10T18:30:00Z")["label"] == "Landed"
