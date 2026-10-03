"""API tests: open board, anonymous ownership, uid proof, authz matrix, rate
limiting, leg caps, OAuth, lifecycle filtering, purge, ETag, security headers.

Manual-airport rows never touch the resolver; flight rows go through the
`fake_resolver` fixture, so the suite runs offline."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from helpers import day, manual_row, seed_airports, session_cookie

import app.auth as auth_mod
import app.main as main_mod
import app.resolver as resolver_mod
from app import config, db, lifecycle
from app.main import app

pytestmark = pytest.mark.usefixtures("fake_resolver")

ADMIN = {"id": "111222333", "name": "Bill", "discord_id": "111222333"}
OWNER = {"id": "424242", "name": "Pat", "discord_id": "424242"}
OTHER = {"id": "555", "name": "Sam", "discord_id": "555"}


def make_trip(c, name="Alex", uid=None, proof=None, legs=None, **extra):
    payload = {"name": name, "out": legs or [manual_row()], **extra}
    if uid:
        payload["uid"] = uid
    if proof:
        payload["proof"] = proof
    return c.post("/api/trips", json=payload)


def trips(c, **kw):
    return c.get("/api/trips", **kw).json()["trips"]


def fmt(t):
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%MZ")


def test_board_reads_without_login(client):
    r = client.get("/api/trips")
    assert r.status_code == 200
    body = r.json()
    assert body["trips"] == [] and body["me"] is None and body["is_admin"] is False
    assert isinstance(body["server_time"], int) and isinstance(body["live_updated_at"], int)


def test_page_renders_open_board(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="login"' not in r.text  # no client id -> no login button


def test_anon_create_edit_delete_with_token(client):
    r = make_trip(client)
    assert r.status_code == 200, r.text
    j = r.json()
    tid, tok = j["trip_id"], j["manage_token"]
    assert j["uid"].startswith("m_")

    body = {"out": [manual_row(to="LHR")]}
    assert client.put(f"/api/trips/{tid}", json=body).status_code == 403
    r = client.put(f"/api/trips/{tid}", json=body, headers={"X-Manage-Token": tok})
    assert r.status_code == 200, r.text
    t = next(x for x in trips(client) if x["id"] == tid)
    assert t["out"][0]["to"] == "EGLL"

    assert client.delete(f"/api/trips/{tid}", headers={"X-Manage-Token": "bogus"}).status_code == 403
    assert client.delete(f"/api/trips/{tid}", headers={"X-Manage-Token": tok}).status_code == 200
    assert client.delete(f"/api/trips/{tid}", headers={"X-Manage-Token": tok}).status_code == 404


def test_anon_create_requires_name(client):
    assert client.post("/api/trips", json={"out": [manual_row()]}).status_code == 400


def test_uid_reuse_requires_proof(client):
    j = make_trip(client).json()
    uid, tok = j["uid"], j["manage_token"]
    assert make_trip(client, uid=uid).json()["uid"] != uid
    assert make_trip(client, uid=uid, proof="wrong").json()["uid"] != uid
    assert make_trip(client, uid=uid, proof=tok).json()["uid"] == uid


def test_leg_cap_rejected_before_resolution(client, fake_resolver):
    legs = [{"flight_no": "DL1200", "date": day(5)}] * (config.MAX_LEGS_PER_TRIP + 1)
    assert make_trip(client, legs=legs).status_code == 400
    assert fake_resolver.calls == []


def test_write_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config, "WRITE_RATE_LIMIT", 5)
    for _ in range(config.WRITE_RATE_LIMIT):
        assert make_trip(client).status_code == 200
    assert make_trip(client).status_code == 429


def test_oauth_state_round_trip(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_CLIENT_ID", "cid")
    monkeypatch.setattr(auth_mod, "is_configured", lambda: True, raising=False)
    c = TestClient(app)
    r = c.get("/login", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "state=" in r.headers["location"]
    r = c.get("/auth/callback?code=x&state=wrong", follow_redirects=False)
    assert "auth=error" in r.headers["location"]


def test_login_unavailable_when_not_configured(monkeypatch):
    monkeypatch.setattr(auth_mod, "is_configured", lambda: False, raising=False)
    r = TestClient(app).get("/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "./?auth=unavailable"


def _login_flow(monkeypatch, exchange):
    monkeypatch.setattr(config, "DISCORD_CLIENT_ID", "cid")
    monkeypatch.setattr(auth_mod, "is_configured", lambda: True, raising=False)
    monkeypatch.setattr(auth_mod, "exchange_code", exchange)
    c = TestClient(app)
    loc = c.get("/login", follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    return c, c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)


def test_oauth_success_sets_session_and_logout_is_post(monkeypatch):
    async def ok(code):
        return {"me": {"id": 777, "username": "pilot7"}, "guilds": []}

    c, r = _login_flow(monkeypatch, ok)
    assert r.status_code in (302, 307) and "auth=" not in r.headers["location"]
    assert "max-age=1209600" in r.headers["set-cookie"].lower()
    assert c.get("/api/trips").json()["me"] == main_mod.public_owner_id("777")
    assert c.get("/logout").status_code == 405
    assert c.post("/logout", follow_redirects=False).status_code == 303
    assert c.get("/api/trips").json()["me"] is None


def test_oauth_discord_error_redirects(monkeypatch):
    async def boom(code):
        raise getattr(auth_mod, "DiscordError", RuntimeError)("nope")

    _, r = _login_flow(monkeypatch, boom)
    assert "auth=error" in r.headers["location"]


def test_authz_matrix():
    anon = TestClient(app)
    j = make_trip(anon, name="Anon").json()
    tid, tok = j["trip_id"], j["manage_token"]
    body = {"out": [manual_row(to="LHR")]}

    def put(c, headers=None):
        return c.put(f"/api/trips/{tid}", json=body, headers=headers or {}).status_code

    assert put(anon) == 403  # anon, no token
    assert put(anon, {"X-Manage-Token": "wrong"}) == 403  # wrong token
    stranger = TestClient(app, cookies=session_cookie(OTHER))
    assert put(stranger) == 403  # logged in, not owner
    admin = TestClient(app, cookies=session_cookie(ADMIN))
    assert put(admin) == 200  # admin
    assert put(anon, {"X-Manage-Token": tok}) == 200  # anon with token

    owner = TestClient(app, cookies=session_cookie(OWNER))  # logged-in owner, no token
    oid = make_trip(owner).json()["trip_id"]
    assert owner.put(f"/api/trips/{oid}", json=body).status_code == 200
    assert stranger.delete(f"/api/trips/{oid}").status_code == 403
    assert owner.delete(f"/api/trips/{oid}").status_code == 200
    assert admin.delete(f"/api/trips/{tid}").status_code == 200


def test_put_404_when_trip_gone(client):
    j = make_trip(client).json()
    client.delete(f"/api/trips/{j['trip_id']}", headers={"X-Manage-Token": j["manage_token"]})
    r = client.put(
        f"/api/trips/{j['trip_id']}",
        json={"out": [manual_row()]},
        headers={"X-Manage-Token": j["manage_token"]},
    )
    assert r.status_code == 404


def test_put_404_when_trip_vanishes_during_resolution(client, fake_resolver, monkeypatch):
    j = make_trip(client).json()
    tid, tok = j["trip_id"], j["manage_token"]

    async def purged_midway(rows, *, deadline=20.0):
        with db.get_conn() as c:  # the trip is purged while we resolve
            c.execute("DELETE FROM trips WHERE id = ?", (tid,))
        return await fake_resolver(rows, deadline=deadline)

    monkeypatch.setattr(resolver_mod, "resolve_rows", purged_midway)
    resp = client.put(
        f"/api/trips/{tid}",
        json={"out": [{"flight_no": "DL1200", "date": day(5)}]},
        headers={"X-Manage-Token": tok},
    )
    assert resp.status_code == 404


def test_put_409_on_concurrent_edit(client, fake_resolver, monkeypatch):
    j = make_trip(client).json()
    tid, tok = j["trip_id"], j["manage_token"]

    async def raced(rows, *, deadline=20.0):
        with db.get_conn() as c:  # someone else edits while we resolve
            c.execute("UPDATE trips SET updated_at = updated_at + 5 WHERE id = ?", (tid,))
        return await fake_resolver(rows, deadline=deadline)

    monkeypatch.setattr(resolver_mod, "resolve_rows", raced)
    resp = client.put(
        f"/api/trips/{tid}",
        json={"out": [{"flight_no": "DL1200", "date": day(5)}]},
        headers={"X-Manage-Token": tok},
    )
    assert resp.status_code == 409


def test_edit_is_one_transaction(client):
    j = make_trip(client, legs=[manual_row(), manual_row(to="LHR")]).json()
    tid, tok = j["trip_id"], j["manage_token"]
    # the second row is invalid, so nothing may change
    r = client.put(
        f"/api/trips/{tid}",
        headers={"X-Manage-Token": tok},
        json={"out": [manual_row(to="LHR"), {"from": "ZZZ", "to": "DEN"}]},
    )
    assert r.status_code == 400
    t = next(x for x in trips(client) if x["id"] == tid)
    assert [l["to"] for l in t["out"]] == ["KDEN", "EGLL"]


def test_edit_preserves_resolved_leg_during_outage(client, fake_resolver):
    fake_resolver.flights["DL1200"] = ("KJAX", "KDEN", f"{day(5)} 12:00Z", f"{day(5)} 16:00Z", "N123DL")
    row = {"flight_no": "DL1200", "date": day(5)}
    j = make_trip(client, legs=[row]).json()
    tid, tok = j["trip_id"], j["manage_token"]
    fake_resolver.override["DL1200"] = "upstream_unavailable"
    r = client.put(f"/api/trips/{tid}", json={"out": [row]}, headers={"X-Manage-Token": tok})
    assert r.status_code == 200, r.text
    leg = next(t for t in trips(client) if t["id"] == tid)["out"][0]
    assert leg["resolved"] is True and leg["reg"] == "N123DL" and leg["unverified"] is False
    # a changed date is a different flight: no preservation, so the outage blocks the save
    r = client.put(
        f"/api/trips/{tid}",
        headers={"X-Manage-Token": tok},
        json={"out": [{"flight_no": "DL1200", "date": day(6)}]},
    )
    assert r.status_code == 400


def test_flight_row_through_resolver_shapes_leg(client, fake_resolver):
    fake_resolver.flights["DL1200"] = ("KJAX", "KDEN", f"{day(5)} 12:00Z", f"{day(5)} 16:00Z", "N123DL")
    r = make_trip(client, legs=[{"flight_no": "dl 1200", "date": day(5)}])
    assert r.status_code == 200, r.text
    assert fake_resolver.calls[0][0]["flight_no"] == "DL1200"
    leg = trips(client)[0]["out"][0]
    assert leg["from"] == "KJAX" and leg["from_city"] == "Jacksonville" and leg["to_city"] == "Denver"
    assert leg["fa_url"] == "https://flightaware.com/live/flight/TST1200"
    assert leg["resolved"] is True and leg["manual"] is False and leg["live_state"] is None
    assert set(leg) >= {"dep_utc", "arr_utc", "unverified", "from_city", "to_city", "live_state", "fa_url"}
    assert "live_url" not in leg and "live" not in leg


def test_missing_date_defaults_to_today(client, fake_resolver):
    r = make_trip(client, legs=[{"flight_no": "DL1200"}, {"from": "JAX", "to": "DEN"}])
    assert r.status_code == 200, r.text
    assert fake_resolver.calls[0][0]["date"] == day(0)
    assert {l["date_local"] for l in trips(client)[0]["out"]} == {day(0)}


def test_manual_leg_has_no_fa_url(client):
    make_trip(client)
    leg = trips(client)[0]["out"][0]
    assert leg["fa_url"] is None and leg["manual"] is True and leg["flight_no"] is None


def test_fa_url_is_quoted():
    assert lifecycle.fa_url("A B/C", None) == "https://flightaware.com/live/flight/A%20B%2FC"
    assert lifecycle.fa_url(None, None) is None


def test_etag_304(client):
    make_trip(client)
    r = client.get("/api/trips")
    etag = r.headers["etag"]
    assert etag and r.headers["cache-control"] == "private, no-cache"
    r2 = client.get("/api/trips", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == etag
    assert client.get("/api/trips", headers={"If-None-Match": f'W/{etag}, "zzz"'}).status_code == 304
    make_trip(client, name="Second")
    r3 = client.get("/api/trips", headers={"If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["etag"] != etag


def test_etag_covers_live_state(client, monkeypatch):
    import app.airplaneslive as al

    now = lifecycle.now()
    with db.get_conn() as c:
        tid = c.execute(
            "INSERT INTO trips (owner_id,owner_name,created_at,ends_at,updated_at) VALUES ('m_x','X',?,?,?)",
            (now, now + 7200, now),
        ).lastrowid
        c.execute(
            "INSERT INTO legs (trip_id,direction,seq,date_local,flight_no,callsign,dep_utc,arr_utc,"
            "resolved,manual) VALUES (?, 'out',0,?, 'DL1','DAL1',?,?,1,0)",
            (tid, day(0), fmt(now - 1800), fmt(now + 3600)),
        )
    monkeypatch.setattr(al, "get_state", lambda cs: "airborne", raising=False)
    a = client.get("/api/trips")
    assert a.json()["trips"][0]["out"][0]["live_state"] == "airborne"
    monkeypatch.setattr(al, "get_state", lambda cs: "on_ground", raising=False)
    b = client.get("/api/trips")
    assert b.json()["trips"][0]["out"][0]["live_state"] == "on_ground"
    assert a.headers["etag"] != b.headers["etag"]
    # outside the live window the state is not reported at all
    with db.get_conn() as c:
        c.execute("UPDATE legs SET dep_utc = ?, arr_utc = ?", (fmt(now + 86400), fmt(now + 90000)))
    assert client.get("/api/trips").json()["trips"][0]["out"][0]["live_state"] is None


def test_airport_search_escapes_wildcards_and_prefixes_first(client):
    assert client.get("/api/airports/search", params={"q": "%%"}).json()["results"] == []
    assert client.get("/api/airports/search", params={"q": "__"}).json()["results"] == []
    res = client.get("/api/airports/search", params={"q": "de"}).json()["results"]
    assert res and res[0]["ident"] == "KDEN"
    assert client.get("/api/airports/search", params={"q": "x" * 500}).status_code == 200


def test_airport_search_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_RATE_LIMIT", 3)
    codes = [client.get("/api/airports/search", params={"q": "den"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_find_airport_iata_lookup_is_deterministic():
    seed_airports(
        [
            ("KZZZ", "ZZZ", "Tiny strip", 1, 1, "small_airport", "A"),
            ("KAAA", "ZZZ", "Big hub", 2, 2, "large_airport", "B"),
            ("KBBB", "ZZZ", "Mid field", 3, 3, "medium_airport", "C"),
        ]
    )
    for _ in range(3):
        assert db.find_airport("ZZZ")["ident"] == "KAAA"


def test_security_headers_present(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "https://*.tile.openstreetmap.org" in csp and "frame-ancestors 'none'" in csp
    assert "form-action 'self' https://discord.com" in csp
    assert r.headers["referrer-policy"] and r.headers["permissions-policy"]
    assert client.get("/api/trips").headers["content-security-policy"] == csp


def test_lifecycle_filter_and_purge(client):
    now = lifecycle.now()
    grace = config.TRIP_GRACE_HOURS * 3600
    with db.get_conn() as c:
        old_id = c.execute(
            "INSERT INTO trips (owner_id, owner_name, created_at, ends_at, updated_at) VALUES ('m_old','Old',?,?,?)",
            (now, now - grace - 3600, now),
        ).lastrowid
        new_id = c.execute(
            "INSERT INTO trips (owner_id, owner_name, created_at, ends_at, updated_at) VALUES ('m_new','New',?,?,?)",
            (now, now + 3600, now),
        ).lastrowid
    ids = {t["id"] for t in trips(client)}
    assert new_id in ids and old_id not in ids
    assert lifecycle.purge_old_trips() == 0  # inside the purge horizon
    with db.get_conn() as c:
        c.execute(
            "UPDATE trips SET ends_at = ? WHERE id = ?", (now - config.TRIP_PURGE_DAYS * 86400 - 10, old_id)
        )
    assert lifecycle.purge_old_trips() == 1
    with db.get_conn() as c:
        assert c.execute("SELECT 1 FROM trips WHERE id = ?", (old_id,)).fetchone() is None
