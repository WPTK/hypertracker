"""Central configuration, loaded from environment / .env."""
import os
from pathlib import Path

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
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
# When true, auth is bypassed with a fake local user so reviewers can run it
# without setting up a Discord application. Never enable in production.
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
# If true, the board is readable without logging in (writes still require auth).
PUBLIC_BOARD = os.getenv("PUBLIC_BOARD", "false").lower() == "true"
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
# Set to e.g. "/board" when hosting under a sub-path behind nginx/Cloudflare.
# Leave empty when serving at the root of a (sub)domain.
ROOT_PATH = os.getenv("ROOT_PATH", "")

# --- Discord OAuth + bot ---
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", BASE_URL + "/auth/callback")
# The single server whose members are allowed to use the site.
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
# Shared secret the bot uses to talk to the backend's internal endpoint.
BOT_SHARED_TOKEN = os.getenv("BOT_SHARED_TOKEN", "change-me-too")
BACKEND_URL = os.getenv("BACKEND_URL", BASE_URL)

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
