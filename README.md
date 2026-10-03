# Hyperfixed Flight Tracker

A shared flight board for a Discord (or any group). People post the trips they're
taking; the board shows, per person, where they're going — with deep links to
track each flight and a real map of every route. Built to live at `adsb.cc/board`.

It's a **link board**, not a polling tracker: each flight is resolved once (route,
times, callsign, tail number, aircraft type + age), cached, and turned into
tracker links. The only thing checked live is whether a flight is *airborne right
now*, so the "Live" link only appears when it actually works.

**No bot required.** You do not need to own or moderate the server. The board runs
entirely as a website. People add trips one of two ways:

- **Manual entry (no login).** Type a name and your flights. The browser quietly
  remembers the trips you create, so you can edit or remove them later — no
  account, nothing to install.
- **Discord login (optional).** Sign in with Discord so your name fills in
  automatically and your trips follow you across devices. This is plain OAuth —
  again, no bot and no moderator rights.

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
   first-flight dates, so we compute age from there.

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

With caching, a person adding a 4-flight trip is ~4 AeroDataBox calls, once.
AeroDataBox's free tier covers a few hundred calls/month and the $5/month tier
covers 3,000 — far more than a small group generates. Realistic spend: **$0/month**,
with $5 as a ceiling. Entering airports manually skips the API entirely, so a
name-only board with hand-entered airports costs nothing and needs no API key.

### Pieces

```
browser ──HTTP──> FastAPI app (app/) ──> SQLite (data/flightboard.db)
                       │  └── AeroDataBox (resolve + aircraft), airplanes.live (live)
                       └── optional Discord OAuth (login only — no bot)
```

The FastAPI app is the single database writer and does all resolution. There is
no separate bot process and no internal write endpoint to secure.

---

## Project layout

```
app/
  main.py          FastAPI app: pages, JSON API, optional Discord OAuth
  config.py        all settings, read from .env
  db.py            SQLite schema + helpers (stdlib sqlite3, no ORM)
  aerodatabox.py   flight resolution + aircraft type/age (cached)
  airplaneslive.py "is this callsign airborne right now?" (cached)
  resolver.py      orchestrates a leg: route, times, callsign, tail, type, age
  lifecycle.py     trip lifecycle: board cutoff, API shaping, purge of old rows
  auth.py          Discord OAuth2 (optional), single-server gate, admins
  templates/board.html   page shell (your adsb.cc design system)
  static/styles.css      styles
  static/app.js          board rendering, highlight, Leaflet map, add/edit modal
scripts/
  update_airports.py     download + load the OurAirports dataset
deploy/
  nginx.conf.example, hyperfixed-web.service
tests/
  conftest.py, test_app.py   offline API tests (pytest)
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

### 3. Run it

```bash
python run.py
# open http://127.0.0.1:8000
```

Out of the box the board is **open**: anyone can view it and add a trip by typing
a name — no Discord setup needed. Flight resolution needs an `AERODATABOX_KEY`;
with no key, resolution simply fails and you can still add legs by hand (the
"Enter airports manually" toggle in the add dialog). For local development you can
set `DEV_MODE=true` to be treated as a logged-in admin.

### 4. (Optional) Add Discord login

Login is optional and only used to auto-fill names/identity and to grant admins.
It is plain OAuth — **you do not need to own or moderate the server, and there is
no bot to invite.** In the
[Discord Developer Portal](https://discord.com/developers/applications):

- Create an application. Copy the **Client ID** and **Client Secret** into `.env`.
- Under **OAuth2 → Redirects**, add your callback URL exactly, e.g.
  `https://adsb.cc/board/auth/callback`, and set the same value as
  `DISCORD_REDIRECT_URI`.
- (Optional) Set `DISCORD_GUILD_ID` to your server's ID to restrict *logging in*
  to members of that one server. Membership is read via the `guilds` OAuth scope —
  still no bot. The open board itself stays public unless you also set
  `OPEN_BOARD=false`.

Leave `DISCORD_CLIENT_ID` blank to hide the login button entirely and run a pure
name-only board.

### 5. (Optional) Admins

Set `ADMIN_DISCORD_IDS` to a comma-separated list of Discord user IDs. Those
people, once logged in, can edit or remove **any** trip on the board (everyone
else can only manage their own). Get your ID via Discord → Settings → Advanced →
Developer Mode, then right-click your name → Copy User ID.

### 6. Deploy

`deploy/hyperfixed-web.service` is a systemd unit — adjust the paths/user, drop it
in `/etc/systemd/system/`, then `systemctl enable --now hyperfixed-web`.
`deploy/nginx.conf.example` has two patterns: a dedicated hostname
(`board.adsb.cc`, simplest) or a `/board` sub-path on your existing site. For a
sub-path, set `ROOT_PATH=/board` and run uvicorn with `--root-path /board`.
Equivalently, point a Cloudflare Tunnel ingress rule at `http://127.0.0.1:8000`.

---

## Design notes

**Open by default.** `OPEN_BOARD=true` (the default) lets anyone view the board and
add their own trips without logging in. Set `OPEN_BOARD=false` to require Discord
login to view and post (pair with `DISCORD_GUILD_ID` for a members-only board).

**Identity & ownership.** A trip is owned either by a logged-in Discord user or by
a name-only "manual" submitter. Manual submitters never create an account: on
their first post the browser is issued a stable local id (so all their trips group
under one name) and, per trip, a secret manage token. Both are stored in the
browser's `localStorage`. The token — held only by that
browser, stored server-side only as its SHA-256 hash — is what authorizes a
later edit or remove. (At 192 random bits the token needs no salt; hashing just
means a leaked database isn't a skeleton key.) After an anonymous save the
dialog also offers a one-time **manage link** as a backup: opening it in any
browser grants the same edit/remove rights — useful if site data gets cleared
or you switch devices.
Editing
changes a trip's flights/airports only; the name/owner stays fixed (an admin can
remove and re-add if a name itself needs changing).

**Who can edit/remove a trip.** Any of: a configured **admin** (any trip), the
**logged-in owner** (their own), or a browser presenting the correct **manage
token** for that trip. There is no other write path.

**Abuse limits.** Writes are rate-limited per client IP (`WRITE_RATE_LIMIT` per
`WRITE_RATE_WINDOW` seconds; admins are exempt). The IP is read from
`CF-Connecting-IP` / `X-Forwarded-For`, since behind the tunnel the TCP peer is
always localhost. A trip is capped at `MAX_LEGS_PER_TRIP` legs, which also caps
AeroDataBox spend per request. Reusing a browser identity on a new trip
requires proof — a manage token that identity already owns — so nobody can post
under someone else's name group just by reading ids off the public API. Trips
that have been off the board for `TRIP_PURGE_DAYS` days are deleted by a daily
sweep (0 = keep forever).

**Time zones.** A person enters a departure *date*, interpreted as the **departure
airport's local date** — which is how AeroDataBox indexes flights and how
schedules are published (overnight flights are disambiguated with
`dateLocalRole=Departure`). Each leg is shown in its own airport-local time. The
submitter's browser time zone is stored alongside the trip for context.

**Lifecycle.** A trip stays on the board until `TRIP_GRACE_HOURS` (default 8) after
its final leg's scheduled arrival, so it lingers through the rest of the arrival
day and then drops off. Trips that couldn't be resolved are always shown so the
owner can fix or remove them.

---

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | The board page |
| `GET` | `/login` · `/auth/callback` · `/logout` | Discord OAuth (optional) |
| `GET` | `/api/trips` | Active trips as JSON (open unless `OPEN_BOARD=false`) |
| `POST` | `/api/trips` | Create a trip. Logged-in: owned by you. No login: include a `name`; the response returns a one-time `manage_token` + `uid` the browser stores |
| `PUT` | `/api/trips/{id}` | Edit a trip's legs (admin, owner, or `X-Manage-Token`) |
| `DELETE` | `/api/trips/{id}` | Remove a trip (admin, owner, or `X-Manage-Token`) |
| `GET` | `/api/airports/search?q=` | Airport typeahead |

Writes are per-IP rate-limited and return `429` past the limit.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs offline — every test leg uses manually entered airports, so
nothing touches AeroDataBox.

---

## Data sources & licensing

- **OurAirports** — public domain. Airport coordinates/codes.
- **AeroDataBox** — commercial API (your key); flight + aircraft data.
- **airplanes.live** — free API, non-commercial, ~1 req/sec; live-status only.
- **FlightAware** — outbound links only.
- **Map tiles** — OpenStreetMap data via CARTO; attribution is shown on the map.

This is a personal, non-commercial project. Check each provider's terms before any
other use.
