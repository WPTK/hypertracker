# Hypertracker

A shared flight board for a friend group. People post the trips they are taking,
and the board shows where everyone is going, with a map of every route, live
airborne / on ground status, and links to track each flight.

It is a small, self-hosted FastAPI app with SQLite and a vanilla JavaScript
front end. No bot, no build step.

## Features

- Add a trip by flight number and date. Each row is checked as you type: you see
  the route, local times, and aircraft before you save.
- Or enter airports by hand. Manual legs never touch a paid API.
- Optional Discord sign-in (plain OAuth, no bot). Without it, the browser keeps a
  private manage token for each trip you add.
- Route map (Leaflet over OpenStreetMap tiles) with great-circle arcs. If tiles or
  Leaflet fail to load, the board still works and the map says so.
- Live status per leg: airborne or on ground, from a background poller.
- Midnight and Daylight themes, built on the WPTK Design System (Midnight Modern).
- Trips tidy themselves: hidden after they end, deleted three days later.

## How it works

**Resolution.** A flight number plus a date is sent to
[AeroDataBox](https://aerodatabox.com) once. The result (route, scheduled times,
callsign, tail number) is cached, and a second cached call per airframe fills in
the aircraft type and age. The resolver returns a typed status rather than
guessing: `ok`, `manual_ok`, `ambiguous`, `not_found`, `out_of_window`, `invalid`,
`airport_unknown`, `upstream_unavailable`, `quota`. If a flight number matches
several legs that day, you pick one. A job re-checks upcoming legs near departure,
when airlines assign tail numbers.

**Caching tiers.**

| What | Lifetime |
|---|---|
| Flights already flown | 30 days |
| Upcoming flights | 6 hours |
| "Not found" answers | 5 minutes |
| Aircraft (type, age) | 90 days |

Previewing and then saving costs one upstream call. Failures are never cached as
answers.

**Live status.** A single background task polls
[airplanes.live](https://airplanes.live) about once per second, one callsign at a
time, only for legs between 20 minutes before departure and 45 minutes after
arrival. `GET /api/trips` reads the result from memory: the board never waits on a
network call. If the API fails, the last known state is kept. A leg is
`airborne`, `on_ground`, or has no state.

**Tracking links.** Each leg links to FlightAware when it has a flight number or
callsign.

## Trip lifecycle

Every trip has an `ends_at`, the latest end over all of its legs. A leg ends at
its scheduled arrival or, when that is unknown (manual legs, unresolved flights),
at the end of its local date plus 36 hours.

- The board hides a trip 8 hours after `ends_at` (`TRIP_GRACE_HOURS`).
- An hourly job deletes it 3 days after `ends_at` (`TRIP_PURGE_DAYS`) and prunes
  old cache rows.

A leg with no date defaults to today.

## Setup

Python 3.11 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # set SECRET_KEY at minimum
python -m scripts.update_airports   # load the OurAirports dataset
python run.py                       # http://127.0.0.1:8000
```

`SECRET_KEY` is required: the app will not start without a real one. For local
hacking, set `DEV_MODE=true` (localhost only) to be treated as an admin.

Re-run `scripts.update_airports` from cron or a systemd timer now and then. It
parses the CSV first and keeps the existing table if the download looks bad.

### Optional: Discord sign-in

Create an application in the
[Discord Developer Portal](https://discord.com/developers/applications), copy the
client id and secret into `.env`, and register the redirect URL
(`DISCORD_REDIRECT_URI`) exactly. Set `DISCORD_GUILD_ID` to limit sign-in to one
server; this adds the `guilds` scope, otherwise only `identify` is requested. Set
`ADMIN_DISCORD_IDS` for people who can edit or remove any trip.

## Configuration

All settings come from the environment (or `.env`). A commented template is in
[.env.example](.env.example).

| Name | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | none, required | Signs cookies, derives owner ids. Startup fails if unset or a placeholder. |
| `DEV_MODE` | `false` | Everyone is a local admin. Aborts unless `BASE_URL` is localhost. |
| `OPEN_BOARD` | `true` | `false` requires login to view and post. |
| `BASE_URL` | `http://localhost:8000` | Public URL. |
| `ROOT_PATH` | empty | Sub-path when hosted under one, e.g. `/board`. |
| `TRUSTED_PROXY` | `none` | `none`, `cloudflare`, or `nginx`. How the client IP is read for rate limits. |
| `DB_PATH` | `data/flightboard.db` | SQLite file. |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | empty | Enables Discord login when set. |
| `DISCORD_REDIRECT_URI` | `BASE_URL/auth/callback` | OAuth callback. |
| `DISCORD_GUILD_ID` | empty | Only members of this server may log in. |
| `ADMIN_DISCORD_IDS` | empty | Comma-separated admin user ids. |
| `AERODATABOX_KEY` | empty | Needed for automatic resolution. |
| `AERODATABOX_BASE`, `AERODATABOX_HOST` | RapidAPI values | Upstream location. |
| `AERODATABOX_AUTH` | `rapidapi` | `rapidapi` or `apimarket` header scheme. |
| `FLIGHT_CACHE_TTL_PAST` | `2592000` | Seconds to cache flights already flown. |
| `FLIGHT_CACHE_TTL_NEAR` | `21600` | Seconds to cache upcoming flights. |
| `FLIGHT_NEGATIVE_TTL` | `300` | Seconds to cache "not found". |
| `AIRCRAFT_CACHE_TTL` | `7776000` | Seconds to cache aircraft. |
| `FLIGHT_WINDOW_PAST_DAYS` | `2` | Oldest accepted flight date, in days before today. |
| `FLIGHT_WINDOW_FUTURE_DAYS` | `330` | Furthest accepted flight date, in days ahead. |
| `UPSTREAM_TIMEOUT` | `8` | Seconds per upstream call. |
| `RESOLVE_DEADLINE` | `20` | Seconds for resolving all rows in a request. |
| `RESOLVE_CONCURRENCY` | `3` | Rows resolved in parallel. |
| `MAX_REFRESH_PER_DAY` | `40` | Budget for near-term re-resolves. |
| `AIRPLANESLIVE_BASE` | airplanes.live v2 | Live status API. |
| `TRIP_GRACE_HOURS` | `8` | Hours on the board after `ends_at`. |
| `TRIP_PURGE_DAYS` | `3` | Days after `ends_at` until deletion. |
| `WRITE_RATE_LIMIT`, `WRITE_RATE_WINDOW` | `12`, `600` | Writes per window per IP. Admins exempt. |
| `PREVIEW_RATE_LIMIT`, `PREVIEW_RATE_WINDOW` | `60`, `600` | Previews per window per IP. |
| `MAX_LEGS_PER_TRIP` | `8` | Legs per trip. |
| `AIRPORTS_CSV_URL` | OurAirports | Source for `scripts.update_airports`. |

## Deployment

Run a single uvicorn worker: live status, rate limits, and the refresh budget are
held in process memory. See [deploy/README.md](deploy/README.md) for the systemd
unit, reverse proxy configuration, and hardening notes. Set `TRUSTED_PROXY` to
match your proxy.

## API summary

Full detail is in [docs/api-contract.md](docs/api-contract.md).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | The board page. |
| `GET` | `/login`, `/auth/callback` | Discord OAuth. Without Discord, `login` redirects to `./?auth=unavailable`. |
| `POST` | `/logout` | Ends the session, 303 back to the board. |
| `GET` | `/api/trips` | Active trips with live states. Supports `ETag` / `If-None-Match`. |
| `POST` | `/api/legs/preview` | Check rows without saving. No database writes. |
| `POST` | `/api/trips` | Create a trip. Anonymous creates return `manage_token` and `uid`. |
| `PUT` | `/api/trips/{id}` | Replace a trip's legs (admin, owner, or `X-Manage-Token`). |
| `DELETE` | `/api/trips/{id}` | Remove a trip (same authorization). |
| `GET` | `/api/airports/search?q=` | Airport typeahead. |

Errors are JSON, `{"detail": ...}`. Malformed bodies return 400, never 500. A row
that could not be verified fails the save unless the client sends
`accept_unverified` and the status is `not_found`, `upstream_unavailable`, or
`quota`; such legs are shown with an unverified badge.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                 # offline, no network
ruff check . && ruff format --check .
```

Browser tests are optional: `pip install -r requirements-e2e.txt`,
`playwright install chromium`, then `pytest -m e2e`. Tests marked `live` call real
upstreams to refresh fixtures and are never run in CI. See [CLAUDE.md](CLAUDE.md)
for testing conventions and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how
the pieces fit.

## Security notes

- `SECRET_KEY` fails fast. A forged cookie must not be possible with a default key.
- `DEV_MODE` aborts unless the base URL is localhost.
- `TRUSTED_PROXY` decides which headers are believed for the client IP. Leave it
  at `none` unless a proxy you control sits in front, and make that proxy
  overwrite the forwarding headers.
- A strict Content-Security-Policy is sent: same-origin scripts and styles only,
  no inline scripts, images also from OpenStreetMap tiles.
- Manage links use a URL fragment (`#manage=<trip>.<token>`), which browsers never
  send to the server, so it stays out of access logs. The server stores only a
  hash of each manage token.
- Writes are rate limited per IP, and trip size is capped.

## Data sources and licensing

- **OurAirports**: public domain airport data.
- **AeroDataBox**: commercial API, your key. Flight and aircraft data.
- **airplanes.live**: free API for non-commercial use, about one request per
  second. Live status only.
- **FlightAware**: outbound links only.
- **Map tiles**: (c) [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors. The board loads standard tiles directly from OpenStreetMap, which
  is acceptable only for light, friends-scale use under the
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
  Attribution is shown on the map. If your board grows, switch to a tile provider
  you are licensed to use.
- **Leaflet**: BSD-2-Clause, vendored in `app/static/vendor/leaflet/`.

This is a personal, non-commercial project. Check each provider's terms before
other use.

## Limitations

- One process only; no horizontal scaling.
- Flight data is as good as AeroDataBox. Tail numbers often appear only a day or
  two before departure.
- Live status needs a callsign and only covers the window around the flight.
- Identity for anonymous users is per browser. Clearing site data loses it unless
  you saved the manage link. A dedicated identity secret is planned.
- The map needs internet access for tiles.
