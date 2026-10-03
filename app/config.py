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
ADMIN_DISCORD_IDS = {
    s.strip() for s in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if s.strip()
}

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
# Per-IP limit on write requests (create/edit/delete). Admins are exempt. The
# app sits behind a Cloudflare Tunnel / nginx, so the client IP is read from
# CF-Connecting-IP / X-Forwarded-For (uvicorn binds to localhost in deployment,
# so those headers can't be spoofed from outside).
WRITE_RATE_LIMIT = int(os.getenv("WRITE_RATE_LIMIT", "12"))     # writes per window
WRITE_RATE_WINDOW = int(os.getenv("WRITE_RATE_WINDOW", "600"))  # seconds
# Upper bound on legs in a single trip. Each unresolved flight number can cost
# an AeroDataBox call, so this also caps API spend per request.
MAX_LEGS_PER_TRIP = int(os.getenv("MAX_LEGS_PER_TRIP", "8"))

# --- Housekeeping ---
# Delete trips this many days after they leave the board (0 = keep forever).
TRIP_PURGE_DAYS = int(os.getenv("TRIP_PURGE_DAYS", "30"))
