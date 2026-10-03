import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import auth, config


@pytest.fixture(autouse=True)
def discord_cfg(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "DISCORD_CLIENT_SECRET", "sekrit-value")
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "")


def install(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        auth, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped), timeout=5))
    return calls


def run(coro):
    return asyncio.run(coro)


def ok_handler(guilds=None):
    def h(request):
        p = request.url.path
        if p.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        if p.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "42", "username": "al"})
        if p.endswith("/guilds"):
            return httpx.Response(200, json=guilds or [])
        return httpx.Response(404)
    return h


def test_is_configured(monkeypatch):
    assert auth.is_configured()
    monkeypatch.setattr(config, "DISCORD_CLIENT_SECRET", "")
    assert not auth.is_configured()
    monkeypatch.setattr(config, "DISCORD_CLIENT_SECRET", "x")
    monkeypatch.setattr(config, "DISCORD_CLIENT_ID", "")
    assert not auth.is_configured()


def test_login_url_scopes(monkeypatch):
    q = parse_qs(urlparse(auth.login_url("st")).query)
    assert q["scope"] == ["identify"] and q["prompt"] == ["none"] and q["state"] == ["st"]
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "999")
    q = parse_qs(urlparse(auth.login_url("st")).query)
    assert q["scope"] == ["identify guilds"]


def test_success_no_guild_fetch(monkeypatch):
    calls = install(monkeypatch, ok_handler())
    res = run(auth.exchange_code("c"))
    assert res["me"]["id"] == "42" and res["guilds"] == []
    assert not any(c.url.path.endswith("/guilds") for c in calls)
    assert calls[1].headers["authorization"] == "Bearer tok"


def test_success_with_guilds(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "7")
    install(monkeypatch, ok_handler([{"id": "7"}]))
    res = run(auth.exchange_code("c"))
    assert auth.in_required_guild(res["guilds"])


def test_token_400_no_secret_leak(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(auth.DiscordError) as ei:
        run(auth.exchange_code("c"))
    msg = str(ei.value)
    assert "400" in msg and "sekrit-value" not in msg and "cid" not in msg


def test_me_429(monkeypatch):
    def h(r):
        if r.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(429, json={"message": "rate limited"})
    install(monkeypatch, h)
    with pytest.raises(auth.DiscordError):
        run(auth.exchange_code("c"))


@pytest.mark.parametrize("token_resp", [
    httpx.Response(200, text="<html>nope"),
    httpx.Response(200, json=["x"]),
    httpx.Response(200, json={"no": "token"}),
])
def test_bad_token_bodies(monkeypatch, token_resp):
    install(monkeypatch, lambda r: token_resp)
    with pytest.raises(auth.DiscordError):
        run(auth.exchange_code("c"))


@pytest.mark.parametrize("me_body", [[], {"username": "x"}, "str"])
def test_me_without_id(monkeypatch, me_body):
    def h(r):
        if r.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, content=json.dumps(me_body))
    install(monkeypatch, h)
    with pytest.raises(auth.DiscordError):
        run(auth.exchange_code("c"))


def test_network_error(monkeypatch):
    def h(r):
        raise httpx.ConnectError("boom https://discord.com?secret=sekrit-value")
    install(monkeypatch, h)
    with pytest.raises(auth.DiscordError) as ei:
        run(auth.exchange_code("c"))
    assert "sekrit-value" not in str(ei.value)


def test_guilds_error_dict(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "7")

    def h(r):
        if r.url.path.endswith("/guilds"):
            return httpx.Response(200, json={"message": "401: Unauthorized", "code": 0})
        return ok_handler()(r)
    install(monkeypatch, h)
    res = run(auth.exchange_code("c"))
    assert res["guilds"] == [] and not auth.in_required_guild(res["guilds"])


def test_guilds_http_error(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "7")

    def h(r):
        if r.url.path.endswith("/guilds"):
            return httpx.Response(429, json={})
        return ok_handler()(r)
    install(monkeypatch, h)
    with pytest.raises(auth.DiscordError):
        run(auth.exchange_code("c"))


def test_more_than_200_guilds_paged(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "450")
    universe = [{"id": str(i)} for i in range(1, 451)]
    seen_after = []

    def h(r):
        if r.url.path.endswith("/guilds"):
            assert r.url.params["limit"] == "200"
            after = int(r.url.params.get("after", 0))
            seen_after.append(after)
            return httpx.Response(200, json=[g for g in universe if int(g["id"]) > after][:200])
        return ok_handler()(r)
    install(monkeypatch, h)
    res = run(auth.exchange_code("c"))
    assert len(res["guilds"]) == 450
    assert seen_after == [0, 200, 400]
    assert auth.in_required_guild(res["guilds"])


def test_guild_paging_is_capped(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "x")
    n = {"i": 0}

    def h(r):
        if r.url.path.endswith("/guilds"):
            n["i"] += 1
            base = n["i"] * 1000
            return httpx.Response(200, json=[{"id": str(base + k)} for k in range(200)])
        return ok_handler()(r)
    install(monkeypatch, h)
    run(auth.exchange_code("c"))
    assert n["i"] == auth.MAX_GUILD_PAGES


def test_in_required_guild_tolerance(monkeypatch):
    assert auth.in_required_guild({"message": "err"})  # unconfigured: no gate
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "7")
    assert not auth.in_required_guild({"message": "err"})
    assert not auth.in_required_guild(None)
    assert not auth.in_required_guild(["junk", 3, {"id": 8}])
    assert auth.in_required_guild(["junk", {"id": 7}])


def test_display_name():
    assert auth.display_name({"global_name": "Al"}) == "Al"
    assert auth.display_name({"username": "bob"}) == "bob"
    assert auth.display_name({}) == "pilot"
    assert auth.display_name({"global_name": "\x00\x1b\n"}) == "pilot"
    assert auth.display_name({"global_name": "a\x00b‮c"}) == "abc"
    assert auth.display_name({"global_name": "x" * 100}) == "x" * 40
    assert auth.display_name({"global_name": 12}) == "pilot"


def test_is_admin(monkeypatch):
    assert not auth.is_admin(None)
    assert auth.is_admin({"discord_id": "111222333"})
    assert not auth.is_admin({"discord_id": "5"})
    assert not auth.is_admin({"id": "dev-user"})
    monkeypatch.setattr(config, "DEV_MODE", True)
    assert auth.is_admin({"id": "dev-user"})
