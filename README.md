# Hyperfixed Flight Tracker

A shared flight board for a Discord server. Members post the trips they're taking;
the board shows, per person, where they're going — with deep links to track each
flight and a real map of every route. Built to live at `adsb.cc/board`.

It's a **link board**, not a polling tracker: each flight is resolved once (route,
times, callsign, tail number, aircraft type + age), cached, and turned into
tracker links. The only thing checked live is whether a flight is *airborne right
now*, so the "Live" link only appears when it actually works.

---

## How it works

Three separate jobs, and only one of them costs anything:

1. **Flight number + date → route, times, callsign, tail number, aircraft type + age.**
   This is [AeroDataBox](https://aerodatabox.com). One cached call resolves the
   flight; if it returns a registration, a second cached call (per airframe) gets
   the aircraft type and age. Aircraft data is cached for months because an
   airframe's type never changes and its age barely moves.

   *Why not AeroAPI for aircraft age?* FlightAware's AeroAPI exposes aircraft
   *type* but **not age / manufacture year** — their support confirms it isn't a
   capability of the API. AeroDataBox's aircraft endpoint returns build/rollout/
   first-flight dates, so we compute age from there. AeroAPI can still be used as a
   free fallback resolver if you want, but it isn't required.

2. **Tracker links — free.** [airplanes.live](https://airplanes.live) is the
   featured link *while a flight is airborne* (its globe is keyed to live
   aircraft, so a link to a not-currently-flying flight shows nothing). We detect
   "in the air now" via the free airplanes.live API by callsign and only then
   surface the Live link. [FlightAware](https://flightaware.com) is the
   always-present link that works for scheduled and historical flights.

3. **The map — free.** Airport coordinates come from
   [OurAirports](https://ourairports.com/data/) (public domain, worldwide, ~78k
   airports, regenerated daily). Routes are drawn as great-circle arcs on a
   [Leaflet](https://leafletjs.com) map over CARTO dark tiles.

### Cost

With caching, a member adding a 4-flight trip is ~4 AeroDataBox calls, once.
AeroDataBox's free tier covers a few hundred calls/month and the $5/month tier
covers 3,000 — far more than a Discord generates. Realistic spend: **$0/month**,
with $5 as a ceiling you'd only hit if the server got large and busy. Entering
airports manually skips the API entirely.

### Pieces

```
browser ──HTTP──> FastAPI app (app/) ──> SQLite (data/flightboard.db)
                       │  └── AeroDataBox (resolve + aircraft), airplanes.live (live)
Discord  ──slash──> bot (bot/) ──POST /api/bot/trips──> FastAPI app
```

The bot is intentionally thin: it never touches the database or any flight API.
It collects a flight number + date from the user and posts to the backend, which
is the single database writer and does all resolution. This avoids SQLite
multi-writer contention and keeps one source of truth.

---

## Project layout

```
app/
  main.py          FastAPI app: pages, JSON API, Discord OAuth
  config.py        all settings, read from .env
  db.py            SQLite schema + helpers (stdlib sqlite3, no ORM)
  aerodatabox.py   flight resolution + aircraft type/age (cached)
  airplaneslive.py "is this callsign airborne right now?" (cached)
  resolver.py      orchestrates a leg: route, times, callsign, tail, type, age
  lifecycle.py     when a trip drops off the board; shapes trips for the API
  auth.py          Discord OAuth2 + "must be a member of THE server" gate
  templates/board.html   page shell (your adsb.cc design system)
  static/styles.css      styles
  static/app.js          board rendering, highlight, Leaflet map, add-trip modal
bot/
  bot.py           /flight slash command -> backend
scripts/
  update_airports.py     download + load the OurAirports dataset
deploy/
  nginx.conf.example, hyperfixed-web.service, hyperfixed-bot.service
```

---

## Setup

### 1. Install

```bash
git clone <your-repo> hyperfixed-flight-tracker
cd hyperfixed-flight-tracker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

### 2. Load the airport dataset

```bash
python -m scripts.update_airports
```

Re-run on a schedule (a weekly cron or systemd timer is plenty).

### 3. Try it without Discord (reviewers start here)

Set `DEV_MODE=true` in `.env` to bypass auth with a fake local user, then:

```bash
python run.py
# open http://127.0.0.1:8000
```

You can add trips immediately. Flight resolution needs an `AERODATABOX_KEY`; with
no key, resolution simply fails and you can still add legs by hand (the "Enter
airports manually" toggle in the add dialog).

### 4. Wire up Discord (for real use)

In the [Discord Developer Portal](https://discord.com/developers/applications):

- Create an application. Copy the **Client ID** and **Client Secret** into `.env`.
- Under **OAuth2 → Redirects**, add your callback URL exactly, e.g.
  `https://adsb.cc/board/auth/callback`, and set the same value as
  `DISCORD_REDIRECT_URI`.
- Under **Bot**, create a bot, copy its **token** into `DISCORD_BOT_TOKEN`.
- Copy your server's ID (enable Developer Mode → right-click server → Copy Server
  ID) into `DISCORD_GUILD_ID`. Login is restricted to members of this server.
- Invite the bot to the server with the `applications.commands` scope so the
  `/flight` command appears.

Set `DEV_MODE=false`, then run the web app and the bot:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000      # add --root-path /board for sub-path hosting
python -m bot.bot
```

---

## Design notes

**Time zones.** A member enters a departure *date*, interpreted as the **departure
airport's local date** — which is how AeroDataBox indexes flights and how
schedules are published (overnight flights are disambiguated with
`dateLocalRole=Departure`). Each leg is shown in its own airport-local time. The
submitter's browser time zone is stored alongside the trip for context.

**Lifecycle.** A trip stays on the board until `TRIP_GRACE_HOURS` (default 8) after
its final leg's scheduled arrival, so it lingers through the rest of the arrival
day and then drops off. Trips that couldn't be resolved are always shown so the
owner can fix or remove them.

**Identity & ownership.** Login is via Discord OAuth (`identify guilds`) and gated
to members of the configured server. People edit and delete only their own trips.
Trips created by the bot are owned by the same Discord ID, so the same person can
manage them on the website.

---

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | The board page |
| `GET` | `/login` · `/auth/callback` · `/logout` | Discord OAuth |
| `GET` | `/api/trips` | Active trips as JSON (login required unless `PUBLIC_BOARD`) |
| `POST` | `/api/trips` | Create a trip (own) |
| `DELETE` | `/api/trips/{id}` | Delete own trip |
| `GET` | `/api/airports/search?q=` | Airport typeahead |
| `POST` | `/api/bot/trips` | Bot-only, guarded by `X-Bot-Token` |

---

## Data sources & licensing

- **OurAirports** — public domain. Airport coordinates/codes.
- **AeroDataBox** — commercial API (your key); flight + aircraft data.
- **airplanes.live** — free API, non-commercial, ~1 req/sec; live-status only.
- **FlightAware** — outbound links only.
- **Map tiles** — OpenStreetMap data via CARTO; attribution is shown on the map.

This is a personal, non-commercial project. Check each provider's terms before any
other use.
