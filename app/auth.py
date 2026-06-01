"""Discord OAuth2 (authorization-code flow) + 'must be a member of THE server' gate.

Scopes: identify guilds. We read the user's guild list and require the configured
guild to be present — that restricts the site to members of your one server,
without you needing to be a moderator of it.
"""
import httpx
from urllib.parse import urlencode
from fastapi import Request, HTTPException
from . import config, db

AUTHORIZE = "https://discord.com/oauth2/authorize"
TOKEN = "https://discord.com/api/oauth2/token"
API = "https://discord.com/api"


def login_url() -> str:
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "prompt": "consent",
    }
    return f"{AUTHORIZE}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN, data=data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        token = r.json()
        access = token["access_token"]
        me = (await client.get(f"{API}/users/@me",
                               headers={"Authorization": f"Bearer {access}"})).json()
        guilds = (await client.get(f"{API}/users/@me/guilds",
                                   headers={"Authorization": f"Bearer {access}"})).json()
    return {"me": me, "guilds": guilds}


def in_required_guild(guilds: list) -> bool:
    if not config.DISCORD_GUILD_ID:
        return True  # no guild configured -> don't gate (useful in dev)
    ids = {str(g.get("id")) for g in guilds if isinstance(g, dict)}
    return str(config.DISCORD_GUILD_ID) in ids


def display_name(me: dict) -> str:
    return me.get("global_name") or me.get("username") or "pilot"


def current_user(request: Request) -> dict | None:
    """Return {'id','name'} from session, or a fake user in DEV_MODE."""
    user = request.session.get("user")
    if user:
        return user
    if config.DEV_MODE:
        return {"id": "dev-user", "name": "dev"}
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user
