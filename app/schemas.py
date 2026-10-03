"""Request models and body parsing. Everything that can be wrong with a request
body is turned into a 400 with the contract's error shape, never a 500 or 422."""
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, field_validator

_TZ_MAX = 64


def _clip(v: Any, n: int):
    if v is None:
        return None
    return v.strip()[:n]


def valid_tz(value: str | None) -> str | None:
    """A valid IANA zone key, or None."""
    if not value or len(value) > _TZ_MAX or value.startswith(("/", ".")) or ".." in value:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return value


class Row(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    flight_no: StrictStr | None = None
    date: StrictStr | None = None
    from_: StrictStr | None = Field(default=None, alias="from")
    to: StrictStr | None = None

    @field_validator("flight_no", mode="after")
    @classmethod
    def _flight(cls, v):
        return _clip(v, 12)

    @field_validator("date", mode="after")
    @classmethod
    def _date(cls, v):
        return _clip(v, 10)

    @field_validator("from_", "to", mode="after")
    @classmethod
    def _airport(cls, v):
        return _clip(v, 10)


class TripIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: StrictStr | None = None
    uid: StrictStr | None = None
    proof: StrictStr | None = None
    tz: StrictStr | None = None
    out: list[Row] = Field(default_factory=list)
    ret: list[Row] = Field(default_factory=list)
    accept_unverified: StrictBool = False

    @field_validator("name", mode="after")
    @classmethod
    def _name(cls, v):
        if v is None:
            return None
        return "".join(ch for ch in v if ch.isprintable()).strip()[:40]

    @field_validator("uid", mode="after")
    @classmethod
    def _uid(cls, v):
        return _clip(v, 64)

    @field_validator("proof", mode="after")
    @classmethod
    def _proof(cls, v):
        return _clip(v, 128)

    @field_validator("tz", mode="after")
    @classmethod
    def _tz(cls, v):
        return valid_tz(v.strip()) if v else None


class PreviewIn(BaseModel):
    """Not used by this module's routes; offered for the preview router."""
    model_config = ConfigDict(extra="ignore")
    rows: list[Row] = Field(default_factory=list)


def _first_error_message(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "invalid request"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
    return f"invalid value for '{loc}'" if loc else "invalid request body"


async def parse_body(request: Request, model: type[BaseModel], *, max_bytes: int) -> BaseModel:
    """Read, size-limit, JSON-decode and validate a request body. Raises
    HTTPException(400/413) with a plain message for every failure."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(413, "request body too large")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(413, "request body too large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "request body must be valid JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "request body must be a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(400, _first_error_message(exc))
