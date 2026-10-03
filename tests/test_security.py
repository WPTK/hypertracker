"""Startup guards, client-IP trust, rate limits (spoofing, global cap), key separation."""
import asyncio
import types

import pytest
from fastapi.testclient import TestClient

import app.airplaneslive as al
from app import config, jobs, netutil
from app.main import app, public_owner_id
from helpers import manual_row, session_cookie

pytestmark = pytest.mark.usefixtures("fake_resolver")


# ---------------- config.validate ----------------
def patch(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v)


def test_validate_accepts_test_config():
    config.validate()


@pytest.mark.parametrize("secret", ["", config.DEFAULT_SECRET_KEY])
def test_refuses_unset_or_default_secret(monkeypatch, secret):
    patch(monkeypatch, SECRET_KEY=secret, DEV_MODE=False, BASE_URL="https://tracker.example.com")
    with pytest.raises(config.ConfigError):
        config.validate()
    patch(monkeypatch, BASE_URL="http://localhost:8000")          # localhost alone is not enough
    with pytest.raises(config.ConfigError):
        config.validate()


def test_default_secret_allowed_only_with_dev_mode_on_localhost(monkeypatch):
    patch(monkeypatch, SECRET_KEY=config.DEFAULT_SECRET_KEY, DEV_MODE=True, BASE_URL="http://127.0.0.1:8000")
    config.validate()
    patch(monkeypatch, BASE_URL="http://localhost:8000")
    config.validate()


def test_dev_mode_refused_on_public_host(monkeypatch):
    patch(monkeypatch, DEV_MODE=True, BASE_URL="https://tracker.example.com")
    with pytest.raises(config.ConfigError):
        config.validate()
    patch(monkeypatch, BASE_URL="http://localhost.evil.com")      # host, not prefix
    with pytest.raises(config.ConfigError):
        config.validate()


def test_bad_trusted_proxy_refused(monkeypatch):
    patch(monkeypatch, TRUSTED_PROXY="whatever")
    with pytest.raises(config.ConfigError):
        config.validate()


def test_app_refuses_to_start_with_default_secret(monkeypatch):
    patch(monkeypatch, SECRET_KEY=config.DEFAULT_SECRET_KEY, DEV_MODE=False)
    with pytest.raises(config.ConfigError):
        with TestClient(app):
            pass


def test_forged_cookie_signed_with_default_secret_is_not_admin():
    forged = session_cookie({"id": "111222333", "name": "x", "discord_id": "111222333"},
                            secret=config.DEFAULT_SECRET_KEY)
    c = TestClient(app, cookies=forged)
    body = c.get("/api/trips").json()
    assert body["is_admin"] is False and body["me"] is None
    genuine = TestClient(app, cookies=session_cookie({"id": "111222333", "name": "x", "discord_id": "111222333"}))
    assert genuine.get("/api/trips").json()["is_admin"] is True


def test_owner_hash_key_differs_from_session_key():
    assert config.derived_key("public-owner-id") != config.SECRET_KEY
    assert config.derived_key("a") != config.derived_key("b")
    import hashlib
    import hmac
    naive = hmac.new(config.SECRET_KEY.encode(), b"42", hashlib.sha256).hexdigest()[:16]
    assert public_owner_id("42") != naive and len(public_owner_id("42")) == 16


# ---------------- lifespan ----------------
def test_lifespan_starts_and_cancels_jobs(monkeypatch):
    seen = {}

    async def fake_poller(supplier, interval=60.0):
        seen["supplier"] = supplier
        try:
            await asyncio.sleep(3600)
        finally:
            seen["cancelled"] = True
    monkeypatch.setattr(al, "run_poller", fake_poller, raising=False)
    with TestClient(app) as c:
        assert c.get("/api/trips").status_code == 200
    assert seen["supplier"] is jobs.callsign_supplier and seen["cancelled"] is True


def test_lifespan_survives_missing_poller(monkeypatch):
    monkeypatch.delattr(al, "run_poller", raising=False)
    with TestClient(app) as c:
        assert c.get("/api/trips").status_code == 200


# ---------------- client IP ----------------
def fake_request(peer, **headers):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=peer),
                                 headers={k.replace("_", "-").lower(): v for k, v in headers.items()})


def test_client_ip_none_ignores_headers(monkeypatch):
    patch(monkeypatch, TRUSTED_PROXY="none")
    r = fake_request("127.0.0.1", x_forwarded_for="1.2.3.4", cf_connecting_ip="5.6.7.8")
    assert netutil.client_ip(r) == "127.0.0.1"


def test_client_ip_cloudflare(monkeypatch):
    patch(monkeypatch, TRUSTED_PROXY="cloudflare")
    assert netutil.client_ip(fake_request("127.0.0.1", cf_connecting_ip="5.6.7.8")) == "5.6.7.8"
    assert netutil.client_ip(fake_request("10.0.0.2", cf_connecting_ip="5.6.7.8")) == "5.6.7.8"
    # untrusted peer: header ignored
    assert netutil.client_ip(fake_request("93.184.216.34", cf_connecting_ip="5.6.7.8")) == "93.184.216.34"
    # garbage header: fall back to the peer
    assert netutil.client_ip(fake_request("127.0.0.1", cf_connecting_ip="not-an-ip")) == "127.0.0.1"
    # X-Forwarded-For is not used in cloudflare mode
    assert netutil.client_ip(fake_request("127.0.0.1", x_forwarded_for="9.9.9.9")) == "127.0.0.1"


def test_client_ip_nginx_rightmost_untrusted_hop(monkeypatch):
    patch(monkeypatch, TRUSTED_PROXY="nginx")
    # client-supplied "1.1.1.1" sits left of the hop nginx appended
    r = fake_request("127.0.0.1", x_forwarded_for="1.1.1.1, 34.120.5.7")
    assert netutil.client_ip(r) == "34.120.5.7"
    r = fake_request("127.0.0.1", x_forwarded_for="1.1.1.1, 34.120.5.7, 10.0.0.5")
    assert netutil.client_ip(r) == "34.120.5.7"       # internal hops skipped
    assert netutil.client_ip(fake_request("127.0.0.1", x_forwarded_for="10.0.0.5")) == "127.0.0.1"
    assert netutil.client_ip(fake_request("127.0.0.1")) == "127.0.0.1"
    assert netutil.client_ip(fake_request("127.0.0.1", x_forwarded_for="1.1.1.1, junk")) == "127.0.0.1"
    # untrusted peer: header ignored
    assert netutil.client_ip(fake_request("93.184.216.34", x_forwarded_for="1.1.1.1")) == "93.184.216.34"
    assert netutil.client_ip(types.SimpleNamespace(client=None, headers={})) == "unknown"


# ---------------- rate limiting end to end ----------------
def post(c, headers=None):
    return c.post("/api/trips", json={"name": "A", "out": [manual_row()]}, headers=headers or {}).status_code


def test_xff_spoof_does_not_bypass_limit_when_proxy_none(monkeypatch):
    patch(monkeypatch, WRITE_RATE_LIMIT=3, TRUSTED_PROXY="none")
    c = TestClient(app, client=("127.0.0.1", 1111))
    codes = [post(c, {"X-Forwarded-For": f"9.9.9.{i}", "CF-Connecting-IP": f"8.8.8.{i}"}) for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_xff_spoof_does_not_bypass_limit_behind_nginx(monkeypatch):
    patch(monkeypatch, WRITE_RATE_LIMIT=3, TRUSTED_PROXY="nginx")
    c = TestClient(app, client=("127.0.0.1", 1111))
    # attacker rotates the left-hand (client-controlled) part; nginx appends the real address
    codes = [post(c, {"X-Forwarded-For": f"9.9.9.{i}, 34.120.5.7"}) for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    # a different real client is counted separately
    assert post(c, {"X-Forwarded-For": "34.120.5.8"}) == 200


def test_untrusted_peer_cannot_use_proxy_headers(monkeypatch):
    patch(monkeypatch, WRITE_RATE_LIMIT=2, TRUSTED_PROXY="nginx")
    c = TestClient(app, client=("93.184.216.34", 1111))
    codes = [post(c, {"X-Forwarded-For": f"34.120.5.{i}"}) for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_global_write_cap_stops_address_rotation(monkeypatch):
    patch(monkeypatch, WRITE_RATE_LIMIT=2, TRUSTED_PROXY="nginx")
    c = TestClient(app, client=("127.0.0.1", 1111))
    cap = 2 * 10
    codes = [post(c, {"X-Forwarded-For": f"34.120.5.{i}"}) for i in range(cap + 3)]
    assert codes[:cap] == [200] * cap and codes[cap:] == [429] * 3


def test_admin_exempt_from_write_limit(monkeypatch):
    patch(monkeypatch, WRITE_RATE_LIMIT=1)
    c = TestClient(app, cookies=session_cookie({"id": "111222333", "name": "B", "discord_id": "111222333"}))
    assert [post(c) for _ in range(4)] == [200] * 4


def test_reset_rate_limits():
    assert netutil.hit("x", "k", 1, 60) is True
    assert netutil.hit("x", "k", 1, 60) is False
    netutil.reset_rate_limits()
    assert netutil.hit("x", "k", 1, 60) is True


def test_rate_limit_window_expires(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(netutil, "now", lambda: t[0])
    assert netutil.hit("w", "k", 1, 60) and not netutil.hit("w", "k", 1, 60)
    t[0] += 61
    assert netutil.hit("w", "k", 1, 60)
