"""API tests: open board, anonymous ownership, uid proof, admin authz,
rate limiting, leg caps, OAuth state, lifecycle filtering, and purge.

All legs are manual airports (zero AeroDataBox calls), so the suite runs
offline. Each test uses its own client IPs to stay clear of the rate limiter.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, lifecycle
import app.auth as auth_mod
import app.main as main_mod
from app.main import app

client = TestClient(app)


def H(ip, extra=None):
    h = {"CF-Connecting-IP": ip}
    if extra:
        h.update(extra)
    return h


def make_trip(ip, name="Alex", uid=None, proof=None, legs=None):
    payload = {"name": name, "out": legs or [{"from": "JAX", "to": "DEN", "date": "2026-07-01"}]}
    if uid:
        payload["uid"] = uid
    if proof:
        payload["proof"] = proof
    return client.post("/api/trips", json=payload, headers=H(ip))


def test_board_reads_without_login():
    r = client.get("/api/trips")
    assert r.status_code == 200
    assert "trips" in r.json()


def test_page_renders_open_board():
    html = client.get("/").text
    assert 'id="openAdd"' in html          # anyone can add
    assert 'id="whoName"' in html          # name field for anonymous posters
    assert 'id="savedPanel"' in html       # manage-link backup panel
    assert 'href="login"' not in html      # no client id -> no login button


def test_anon_create_edit_delete_with_token():
    r = make_trip("10.1.0.1")
    assert r.status_code == 200
    j = r.json()
    tid, tok = j["trip_id"], j["manage_token"]
    assert j["uid"].startswith("m_")

    # edit without the token is blocked
    body = {"out": [{"from": "JAX", "to": "LHR", "date": "2026-07-01"}]}
    assert client.put(f"/api/trips/{tid}", json=body, headers=H("10.1.0.2")).status_code == 403
    # with the token it succeeds
    r = client.put(f"/api/trips/{tid}", json=body, headers=H("10.1.0.3", {"X-Manage-Token": tok}))
    assert r.status_code == 200
    t = next(x for x in client.get("/api/trips").json()["trips"] if x["id"] == tid)
    assert t["out"][0]["to"] == "EGLL"

    # wrong token can't delete; right token can
    assert client.delete(f"/api/trips/{tid}", headers=H("10.1.0.4", {"X-Manage-Token": "bogus"})).status_code == 403
    assert client.delete(f"/api/trips/{tid}", headers=H("10.1.0.5", {"X-Manage-Token": tok})).status_code == 200


def test_anon_create_requires_name():
    r = client.post("/api/trips",
                    json={"out": [{"from": "JAX", "to": "DEN", "date": "2026-07-02"}]},
                    headers=H("10.2.0.1"))
    assert r.status_code == 400


def test_uid_reuse_requires_proof():
    j = make_trip("10.3.0.1").json()
    uid, tok = j["uid"], j["manage_token"]

    assert make_trip("10.3.0.2", uid=uid).json()["uid"] != uid              # no proof
    assert make_trip("10.3.0.3", uid=uid, proof="wrong").json()["uid"] != uid  # bad proof
    assert make_trip("10.3.0.4", uid=uid, proof=tok).json()["uid"] == uid   # real proof


def test_leg_cap_rejected_before_resolution():
    legs = [{"from": "JAX", "to": "DEN", "date": "2026-07-01"}] * (config.MAX_LEGS_PER_TRIP + 1)
    assert make_trip("10.4.0.1", legs=legs).status_code == 400


def test_write_rate_limit():
    ip = "10.5.0.1"
    for _ in range(config.WRITE_RATE_LIMIT):
        assert make_trip(ip).status_code == 200
    assert make_trip(ip).status_code == 429


def test_oauth_state_round_trip():
    c = TestClient(app)
    r = c.get("/login", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "state=" in r.headers["location"]
    # A callback whose state doesn't match the session must not complete.
    r = c.get("/auth/callback?code=x&state=wrong", follow_redirects=False)
    assert "auth=error" in r.headers["location"]


def test_admin_authz_unit():
    admin = {"id": "111222333", "name": "Bill", "discord_id": "111222333"}
    rando = {"id": "555", "name": "Sam", "discord_id": "555"}
    assert auth_mod.is_admin(admin)
    assert not auth_mod.is_admin(rando)

    class Req:  # minimal stand-in: _authorize_manage only touches .session
        def __init__(self, u):
            self.session = {"user": u} if u else {}

    row = {"owner_id": "m_someoneelse", "manage_token": None}
    main_mod._authorize_manage(Req(admin), row, None)  # admin: any trip
    with pytest.raises(Exception):
        main_mod._authorize_manage(Req(rando), row, None)  # stranger: blocked


def test_lifecycle_filter_and_purge():
    now = int(time.time())
    grace = config.TRIP_GRACE_HOURS * 3600
    with db.get_conn() as c:
        old_id = c.execute(
            "INSERT INTO trips (owner_id, owner_name, created_at, ends_at) VALUES ('m_old','Old',?,?)",
            (now, now - grace - 3600),
        ).lastrowid
        new_id = c.execute(
            "INSERT INTO trips (owner_id, owner_name, created_at, ends_at) VALUES ('m_new','New',?,?)",
            (now, now + 3600),
        ).lastrowid

    ids = {t["id"] for t in client.get("/api/trips").json()["trips"]}
    assert new_id in ids and old_id not in ids

    # push the old trip past the purge horizon and sweep
    with db.get_conn() as c:
        c.execute("UPDATE trips SET ends_at = ? WHERE id = ?",
                  (now - grace - config.TRIP_PURGE_DAYS * 86400 - 10, old_id))
    assert lifecycle.purge_old_trips() >= 1
    with db.get_conn() as c:
        assert c.execute("SELECT 1 FROM trips WHERE id = ?", (old_id,)).fetchone() is None
