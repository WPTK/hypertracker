"""Small helpers shared by the test modules (imported as `helpers`; conftest
sets the environment before anything here touches the app)."""
import base64
import datetime as dt
import json

AIRPORTS = [
    # ident, iata, name, lat, lon, type, municipality
    ("KJAX", "JAX", "Jacksonville Intl", 30.49, -81.69, "large_airport", "Jacksonville"),
    ("KDEN", "DEN", "Denver Intl", 39.86, -104.67, "large_airport", "Denver"),
    ("EGLL", "LHR", "Heathrow", 51.47, -0.45, "large_airport", "London"),
]


def day(offset: int = 0) -> str:
    """ISO date `offset` days from today (UTC)."""
    return (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=offset)).isoformat()


def manual_row(date=None, frm="JAX", to="DEN"):
    return {"from": frm, "to": to, "date": date or day(5)}


def seed_airports(extra=()):
    from app import db
    with db.get_conn() as c:
        for ident, iata, name, lat, lon, typ, city in (*AIRPORTS, *extra):
            c.execute(
                "INSERT OR REPLACE INTO airports (ident,iata,name,lat,lon,type,iso_country,municipality) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ident, iata, name, lat, lon, typ, "US", city),
            )


def session_cookie(user: dict, secret: str | None = None) -> dict:
    """A signed Starlette session cookie for `user` (what a real login sets)."""
    import itsdangerous
    from app import config
    signer = itsdangerous.TimestampSigner(secret or config.SECRET_KEY)
    data = base64.b64encode(json.dumps({"user": user}).encode("utf-8"))
    return {"session": signer.sign(data).decode("utf-8")}
