import datetime as dt

import pytest

from app import validation as v

TODAY = dt.date(2026, 10, 3)


@pytest.mark.parametrize("s", ["DL1200", "dl 1200", "DL-1200", "3K123", "6E2001", "DAL1200", "UA1A", "BA12B"])
def test_valid_flight_numbers(s):
    assert v.is_valid_flight_no(s)


@pytest.mark.parametrize("s", ["", "D", "1200", "DL", "11200", "../../x", "DL1200/x", "DL12345", "DAL", "D L"])
def test_invalid_flight_numbers(s):
    assert not v.is_valid_flight_no(s)


def test_airport_codes():
    assert v.normalize_airport_code(" jax ") == "JAX"
    assert v.normalize_airport_code("KJAX") == "KJAX"
    assert v.normalize_airport_code("J") is None
    assert v.normalize_airport_code("JAX/1") is None
    assert v.normalize_airport_code(None) is None


def test_date_defaults_to_today():
    assert v.validate_leg_date("", TODAY) == ("2026-10-03", None)
    assert v.validate_leg_date(None, TODAY) == ("2026-10-03", None)


def test_date_rejects_garbage_and_window():
    assert v.validate_leg_date("2026-13-45", TODAY)[0] is None
    assert v.validate_leg_date("../../x", TODAY)[0] is None
    assert v.validate_leg_date("2025-01-01", TODAY)[0] is None
    assert v.validate_leg_date("2030-01-01", TODAY)[0] is None
    assert v.validate_leg_date("2026-10-01", TODAY) == ("2026-10-01", None)
    assert v.validate_leg_date("2026-10-10", TODAY) == ("2026-10-10", None)
