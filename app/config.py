"""Central configuration, loaded from environment / .env."""

import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "flightboard.db"))

# --- Session / app ---
DEFAULT_SECRET_KEY = "change-me-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
# When true, auth is bypassed with a fake local admin so reviewers can run it
# without setting up a Discord application. Never enable in production.
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
# Open board (the default): anyone can view the board and add their own trips by
# typing a name — no login required. The browser remembers the trips it created so
# the person can edit/remove them later. Set OPEN_BOARD=false to require Discord
# login to view and post (the old members-only behaviour; pair with
# DISCORD_GUILD_ID to gate to a single server).
OPEN_BOARD = os.getenv("OPEN_BOARD", "true").lower() == "true"
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
# Set to e.g. "/board" when hosting under a sub-path behind nginx/Cloudflare.
# Leave empty when serving at the root of a (sub)domain.
ROOT_PATH = os.getenv("ROOT_PATH", "")

# --- Discord OAuth (optional) ---
# Logging in with Discord is optional. It's plain OAuth — there is NO bot, and you
# do NOT need to own or moderate the server. It's used only to (a) auto-fill a
# person's name/identity so they can manage their trips from any device, and
# (b) grant admins the ability to edit/remove any trip (see ADMIN_DISCORD_IDS).
# Leave DISCORD_CLIENT_ID blank to hide the login button entirely and run a
# pure name-only board.
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", BASE_URL + "/auth/callback")
# Optional: if set, only members of this one server may *log in* (the open board
# itself stays public). Membership is read via the `guilds` OAuth scope — still
# no bot, still no moderator rights required.
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")

# --- Admins ---
# Comma-separated Discord user IDs allowed to edit or remove ANY trip on the
# board. (Discord → Settings → Advanced → Developer Mode, then right-click your
# name → Copy User ID.) Admins must be logged in via Discord for this to apply.
ADMIN_DISCORD_IDS = {s.strip() for s in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if s.strip()}

# --- AeroDataBox (resolution + aircraft type/age) ---
# Defaults target RapidAPI. To use API.Market or direct access, override BASE
# and supply the appropriate header via AERODATABOX_KEY / AERODATABOX_HOST.
AERODATABOX_BASE = os.getenv("AERODATABOX_BASE", "https://aerodatabox.p.rapidapi.com")
AERODATABOX_KEY = os.getenv("AERODATABOX_KEY", "")
AERODATABOX_HOST = os.getenv("AERODATABOX_HOST", "aerodatabox.p.rapidapi.com")

# Cache lifetimes (seconds). Flights are effectively immutable once flown;
# aircraft type/age barely changes, so both are cached aggressively.
FLIGHT_CACHE_TTL = int(os.getenv("FLIGHT_CACHE_TTL", str(60 * 60 * 24 * 30)))  # 30 days
AIRCRAFT_CACHE_TTL = int(os.getenv("AIRCRAFT_CACHE_TTL", str(60 * 60 * 24 * 90)))  # 90 days

# --- airplanes.live (live-status for the "Live" link) ---
AIRPLANESLIVE_BASE = os.getenv("AIRPLANESLIVE_BASE", "https://api.airplanes.live/v2")

# --- Lifecycle ---
# A trip stays on the board until this many hours after its final leg's
# scheduled arrival (so it lingers through the rest of the arrival day).
TRIP_GRACE_HOURS = int(os.getenv("TRIP_GRACE_HOURS", "8"))

# --- OurAirports dataset ---
AIRPORTS_CSV_URL = os.getenv(
    "AIRPORTS_CSV_URL",
    "https://davidmegginson.github.io/ourairports-data/airports.csv",
)

# --- Write protection (open-board hardening) ---
# Per-IP limit on write requests (create/edit/delete). Admins are exempt. Which
# address a request is attributed to is decided by TRUSTED_PROXY (see below);
# forwarding headers are ignored unless the TCP peer is a local/private proxy.
# A global cap of 10x the per-IP limit applies across all addresses.
WRITE_RATE_LIMIT = int(os.getenv("WRITE_RATE_LIMIT", "12"))  # writes per window
WRITE_RATE_WINDOW = int(os.getenv("WRITE_RATE_WINDOW", "600"))  # seconds
# Upper bound on legs in a single trip. Each unresolved flight number can cost
# an AeroDataBox call, so this also caps API spend per request.
MAX_LEGS_PER_TRIP = int(os.getenv("MAX_LEGS_PER_TRIP", "8"))

# --- Housekeeping ---
# Hard-delete trips this many days after their ends_at (0 = keep forever).
TRIP_PURGE_DAYS = int(os.getenv("TRIP_PURGE_DAYS", "3"))

# --- Proxy trust ---
# none: use the TCP peer. cloudflare: honour CF-Connecting-IP when the peer is a
# loopback/private address. nginx: use the right-most X-Forwarded-For hop that
# is not loopback/private, when the peer is loopback/private.
TRUSTED_PROXY = os.getenv("TRUSTED_PROXY", "none").strip().lower()

# --- Flight resolution knobs (read by the resolver with getattr defaults) ---
FLIGHT_CACHE_TTL_PAST = int(os.getenv("FLIGHT_CACHE_TTL_PAST", "2592000"))
FLIGHT_CACHE_TTL_NEAR = int(os.getenv("FLIGHT_CACHE_TTL_NEAR", "21600"))
FLIGHT_NEGATIVE_TTL = int(os.getenv("FLIGHT_NEGATIVE_TTL", "600"))
FLIGHT_WINDOW_PAST_DAYS = int(os.getenv("FLIGHT_WINDOW_PAST_DAYS", "2"))
FLIGHT_WINDOW_FUTURE_DAYS = int(os.getenv("FLIGHT_WINDOW_FUTURE_DAYS", "330"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "8"))
RESOLVE_DEADLINE = float(os.getenv("RESOLVE_DEADLINE", "20"))
RESOLVE_CONCURRENCY = int(os.getenv("RESOLVE_CONCURRENCY", "3"))
AERODATABOX_AUTH = os.getenv("AERODATABOX_AUTH", "rapidapi").strip().lower()  # rapidapi | apimarket
MAX_REFRESH_PER_DAY = int(os.getenv("MAX_REFRESH_PER_DAY", "40"))

# --- Looser rate limits (no DB writes): leg preview and airport search ---
PREVIEW_RATE_LIMIT = int(os.getenv("PREVIEW_RATE_LIMIT", "60"))
PREVIEW_RATE_WINDOW = int(os.getenv("PREVIEW_RATE_WINDOW", "600"))
SEARCH_RATE_LIMIT = int(os.getenv("SEARCH_RATE_LIMIT", "120"))
SEARCH_RATE_WINDOW = int(os.getenv("SEARCH_RATE_WINDOW", "60"))

# Request bodies above this many bytes are refused (413) before parsing.
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(64 * 1024)))

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(RuntimeError):
    """Raised at startup when the configuration is unsafe to run with."""


def base_url_host() -> str:
    try:
        return (urlparse(BASE_URL).hostname or "").lower()
    except ValueError:
        return ""


def is_local_base_url() -> bool:
    return base_url_host() in _LOCAL_HOSTS


def derived_key(label: str) -> str:
    """A key for a specific purpose, derived from SECRET_KEY, so that the session
    signing key and (for example) the owner-id hashing key are different."""
    return hmac.new(SECRET_KEY.encode("utf-8"), f"hypertracker:{label}".encode(), hashlib.sha256).hexdigest()


def validate() -> None:
    """Fail fast on unsafe configuration. Called from the app lifespan; reads
    the module globals at call time so tests can patch them."""
    local = is_local_base_url()
    if DEV_MODE and not local:
        raise ConfigError(
            "DEV_MODE is on but BASE_URL host is not localhost/127.0.0.1: every visitor "
            "would be an admin. Set DEV_MODE=false, or BASE_URL to a local address."
        )
    if not SECRET_KEY or SECRET_KEY == DEFAULT_SECRET_KEY:
        if not (DEV_MODE and local):
            raise ConfigError(
                "SECRET_KEY is unset or the default. Set SECRET_KEY to a long random "
                "value (DEV_MODE on localhost is the only exception)."
            )
    if TRUSTED_PROXY not in ("none", "cloudflare", "nginx"):
        raise ConfigError(f"TRUSTED_PROXY must be none, cloudflare or nginx (got {TRUSTED_PROXY!r}).")
    if AERODATABOX_AUTH not in ("rapidapi", "apimarket"):
        raise ConfigError(f"AERODATABOX_AUTH must be rapidapi or apimarket (got {AERODATABOX_AUTH!r}).")
