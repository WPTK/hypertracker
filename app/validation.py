"""Pure input validators shared by the API and the resolver. No I/O, no config
imports, so they are trivially unit-testable and safe to import anywhere."""
import datetime as dt
import re

# IATA: two alphanumerics (at least one letter, e.g. DL, 3K, 6E) + 1-4 digits + optional suffix.
_IATA_RE = re.compile(r"^(?=[A-Z0-9]{0,1}[A-Z])[A-Z0-9]{2}\d{1,4}[A-Z]?$")
# ICAO airline designator: three letters + 1-4 digits + optional suffix (DAL1200).
_ICAO_RE = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")
_AIRPORT_RE = re.compile(r"^[A-Z0-9]{3,4}$")

DEFAULT_PAST_DAYS = 2        # allow a flight that already left today / yesterday
DEFAULT_FUTURE_DAYS = 330    # [verify] AeroDataBox schedule horizon


def normalize_flight_no(s: str | None) -> str:
    """Uppercase, drop spaces/hyphens/underscores. Does not validate."""
    return re.sub(r"[\s\-_]", "", str(s or "")).upper()


def is_valid_flight_no(s: str | None) -> bool:
    n = normalize_flight_no(s)
    return bool(_IATA_RE.match(n) or _ICAO_RE.match(n))


def normalize_airport_code(s: str | None) -> str | None:
    """Return an uppercase 3-4 char code, or None when it can't be one."""
    n = re.sub(r"\s", "", str(s or "")).upper()
    return n if _AIRPORT_RE.match(n) else None


def validate_leg_date(raw: str | None, today: dt.date,
                      past_days: int = DEFAULT_PAST_DAYS,
                      future_days: int = DEFAULT_FUTURE_DAYS) -> tuple[str | None, str | None]:
    """Return (iso_date, error). A missing date defaults to `today`, so adding a
    flight never fails for lack of one. Garbage or out-of-window dates give an
    error message suitable for showing to the user."""
    s = (raw or "").strip()
    if not s:
        return today.isoformat(), None
    try:
        d = dt.date.fromisoformat(s[:10]) if len(s) >= 10 else dt.date.fromisoformat(s)
    except ValueError:
        return None, "That date isn't valid. Use the date picker."
    if d < today - dt.timedelta(days=past_days):
        return None, "That date is too far in the past."
    if d > today + dt.timedelta(days=future_days):
        return None, "That date is further out than flight schedules go."
    return d.isoformat(), None
