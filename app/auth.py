"""Discord OAuth2 (authorization-code flow), optional single-server gate, admins.

Scopes: `identify`, plus `guilds` only when DISCORD_GUILD_ID is set. Login is
optional — see config.OPEN_BOARD. When a DISCORD_GUILD_ID is configured we read
the user's guild list and require that guild to be present, which restricts
*logging in* to members of your one server without you needing to be a
moderator of it. The board itself stays open.
"""

from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

from . import config

AUTHORIZE = "https://discord.com/oauth2/authorize"
TOKEN = "https://discord.com/api/oauth2/token"
API = "https://discord.com/api"

HTTP_TIMEOUT = 10.0
GUILD_PAGE = 200
MAX_GUILD_PAGES = 10  # sane cap: 2000 guilds
NAME_MAX = 40


class DiscordError(Exception):
    """Discord login failed. The message is safe to log: it never holds secrets."""


def is_configured() -> bool:
    return bool(config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET)


def _gated() -> bool:
    return bool(config.DISCORD_GUILD_ID)


def login_url(state: str) -> str:
    """Authorization URL. `state` is a per-attempt CSRF token: stored in the
    session at /login and verified at /auth/callback, so a login can only
    complete if it started here (prevents login-CSRF)."""
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds" if _gated() else "identify",
        "prompt": "none",
        "state": state,
    }
    return f"{AUTHORIZE}?{urlencode(params)}"


def _make_client() -> httpx.AsyncClient:
    """Short-lived client per login. Tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _json(r: httpx.Response, what: str):
    if not 200 <= r.status_code < 300:
        raise DiscordError(f"Discord {what} returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise DiscordError(f"Discord {what} returned a non-JSON response") from None


async def exchange_code(code: str) -> dict:
    """Trade an OAuth code for {'me': {...}, 'guilds': [...]}.

    Raises DiscordError on any network failure, non-2xx status, non-JSON or
    unexpected body. Guilds are only fetched when DISCORD_GUILD_ID is set."""
    data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
    }
    try:
        async with _make_client() as client:
            r = await client.post(TOKEN, data=data)
            token = _json(r, "token exchange")
            access = token.get("access_token") if isinstance(token, dict) else None
            if not access or not isinstance(access, str):
                raise DiscordError("Discord token exchange returned no access token")
            auth = {"Authorization": f"Bearer {access}"}
            me = _json(await client.get(f"{API}/users/@me", headers=auth), "profile lookup")
            if not isinstance(me, dict) or not me.get("id"):
                raise DiscordError("Discord profile lookup returned no user id")
            guilds = await _fetch_guilds(client, auth) if _gated() else []
    except httpx.HTTPError as e:
        # Only the exception class: httpx messages can echo URLs.
        raise DiscordError(f"Discord request failed ({type(e).__name__})") from None
    return {"me": me, "guilds": guilds}


async def _fetch_guilds(client: httpx.AsyncClient, auth: dict) -> list:
    out: list = []
    after = None
    for _ in range(MAX_GUILD_PAGES):
        params = {"limit": GUILD_PAGE}
        if after:
            params["after"] = after
        r = await client.get(f"{API}/users/@me/guilds", headers=auth, params=params)
        page = _json(r, "guild lookup")
        if not isinstance(page, list):
            break  # error-shaped body: treat as "no (more) guilds"
        out.extend(g for g in page if isinstance(g, dict))
        if len(page) < GUILD_PAGE:
            break
        last = page[-1]
        after = last.get("id") if isinstance(last, dict) else None
        if not after:
            break
    return out


def in_required_guild(guilds) -> bool:
    if not config.DISCORD_GUILD_ID:
        return True  # no guild configured -> don't gate (useful in dev)
    if not isinstance(guilds, list):
        return False
    ids = {str(g.get("id")) for g in guilds if isinstance(g, dict)}
    return str(config.DISCORD_GUILD_ID) in ids


def display_name(me: dict) -> str:
    raw = me.get("global_name") or me.get("username") or ""
    if not isinstance(raw, str):
        raw = ""
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:NAME_MAX].strip() or "pilot"


def is_admin(user: dict | None) -> bool:
    """True if the logged-in user is configured as a board admin (or in DEV_MODE)."""
    if not user:
        return False
    if config.DEV_MODE and user.get("id") == "dev-user":
        return True
    did = user.get("discord_id")
    return bool(did) and str(did) in config.ADMIN_DISCORD_IDS


def current_user(request: Request) -> dict | None:
    """Return {'id','name','discord_id'} from the session, or a fake admin in DEV_MODE."""
    user = request.session.get("user")
    if user:
        return user
    if config.DEV_MODE:
        return {"id": "dev-user", "name": "dev", "discord_id": "dev-user"}
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user
