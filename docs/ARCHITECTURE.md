# Architecture

Single-process FastAPI app, SQLite, vanilla JS. One uvicorn worker.

```
browser ──HTTP──> reverse proxy ──> FastAPI (app/) ──> SQLite (data/flightboard.db)
                                       │
                                       ├── AeroDataBox      (resolve flights, aircraft)   request path + refresh job
                                       ├── airplanes.live   (airborne / on ground)        background poller only
                                       └── Discord OAuth    (optional login)              login/callback only
```

## Module map

| Module | Role |
|---|---|
| `app/main.py` | App factory, middleware (CSP, security headers), lifespan, routes |
| `app/config.py` | Settings from env, validated at startup (secret key, dev mode guard) |
| `app/validation.py` | Normalise and validate flight numbers, airport codes, dates |
| `app/resolver.py` | `resolve_rows`, `refresh_leg`: rows to legs with typed status |
| `app/aerodatabox.py` | Upstream client, response checks, flight and aircraft caches |
| `app/airplaneslive.py` | `get_state` (memory read), `run_poller` (background) |
| `app/lifecycle.py` | `compute_ends_at`, board cutoff, `shape_trip` for the API |
| `app/jobs.py` | Hourly housekeeping loop, `refresh_upcoming` |
| `app/auth.py` | Discord OAuth: `login_url`, `exchange_code`, guild gate |
| `app/netutil.py` | `client_ip` with `TRUSTED_PROXY` handling |
| `app/db.py` | Connection helper, schema, migrations |
| `app/templates/board.html` | Page shell; config in a JSON script block |
| `app/static/js/` | `main.js`, `api.js`, `identity.js`, `map.js`, `tripForm.js` |
| `app/static/css/`, `wptk/`, `vendor/leaflet/` | Styles, vendored design system, vendored Leaflet |
| `scripts/update_airports.py` | Load OurAirports into `airports` |

## Request flows

### Board read

```
GET /api/trips  (If-None-Match: etag)
  └─ authenticate (cookie) ─> read active trips + legs from SQLite
       └─ shape_trip: for each leg, airplaneslive.get_state(callsign)   [memory only]
            └─ compute ETag over trips + live states
                 ├─ matches ─> 304
                 └─ differs ─> 200 { trips, me, is_admin, server_time, live_updated_at }
```

No network call and no write happens here. The client polls and sends the ETag.

### Preview

```
POST /api/legs/preview { rows }
  └─ rate limit (PREVIEW_*) ─> parse rows (pydantic) ─> normalise/validate each
       └─ resolver.resolve_rows (concurrent, semaphore, RESOLVE_DEADLINE)
            ├─ manual row: both airports must exist ─> manual_ok | airport_unknown
            └─ flight row: cache hit? ─> else AeroDataBox ─> check payload ─> cache
                 └─ filter by date, apply from/to hints ─> ok | ambiguous | not_found | ...
  └─ 200 { results: [{ index, status, message, leg, candidates }] }     [no DB writes]
```

### Create (and edit)

```
POST /api/trips   (PUT /api/trips/{id} adds an authorization check)
  └─ rate limit (WRITE_*) ─> parse body ─> identity / proof check
       └─ resolve_rows (reads the same cache, so previewed rows cost nothing)
            ├─ any row not ok/manual_ok and not acceptable ─> 400 { detail: { message, rows } }
            └─ else ─> BEGIN
                         insert/replace legs, compute ends_at (max over all legs)
                       COMMIT   ─> 200 { ok, trip_id [, manage_token, uid] }
```

`accept_unverified` only rescues `not_found`, `upstream_unavailable`, `quota`.

### Background jobs

```
lifespan start
  ├─ jobs.run_housekeeping_loop   hourly: purge trips past ends_at + RETENTION,
  │                               prune caches and orphan users   (asyncio.to_thread)
  ├─ airplaneslive.run_poller     every ~60s: callsigns of legs in
  │                               [dep - 20 min, arr + 45 min], probed one at a time, >= 1 s apart;
  │                               failures keep the last state
  └─ jobs.refresh_upcoming        legs departing within 48h with no registration,
                                  refresh_leg bypassing cache, budget MAX_REFRESH_PER_DAY
```

## Data model

SQLite, `PRAGMA user_version` migrations.

| Table | Purpose |
|---|---|
| `trips` | One per submission: owner, display name, `tz`, manage token hash, `created_at`, `ends_at` (NOT NULL) |
| `legs` | Per trip: `direction` (out/ret), `seq`, date, flight no, callsign, airports (ICAO, IATA, coords), times, aircraft columns, `resolved`, `manual`, `unverified` |
| `airports` | OurAirports subset (ident, iata, name, lat, lon, country, municipality) |
| `flight_cache` | Upstream flight responses keyed by flight and date, with expiry |
| `aircraft_cache` | Upstream aircraft records keyed by registration, with expiry |
| `users` | Discord users seen at login |
| `identities` | PLANNED (Phase 4): `id`, `secret_hash`; replaces proof via trip tokens |

Live state is not stored: it is an in-memory map owned by `airplaneslive`.

## Failure modes

| Failure | Behaviour |
|---|---|
| Missing or placeholder `SECRET_KEY` | Startup aborts |
| `DEV_MODE` with a non-local `BASE_URL` | Startup aborts |
| AeroDataBox down, timeout, 5xx | Status `upstream_unavailable`; not cached; can be saved as unverified |
| AeroDataBox 429 or bad key | Status `quota`; calls paused (circuit breaker); can be saved as unverified |
| Flight not found | Status `not_found`, cached 5 min; can be saved as unverified |
| Junk 200 body | Rejected before caching; treated as upstream failure |
| Several legs match | Status `ambiguous` with candidates; save refused until picked |
| airplanes.live down | Last known live state kept; board unaffected |
| Resolve exceeds `RESOLVE_DEADLINE` | Remaining rows return `upstream_unavailable` |
| Map tiles or Leaflet fail | Visible message in the map pane; board continues |
| Board poll fails (client) | Old data kept, stale banner shown |
| Housekeeping error | Logged with traceback; loop continues |
| Trip deleted during an edit | 404, not 500 |
| Discord returns an error | Clean error redirect; no 500 |
| Process restart | Live state empty until the next poll cycle; everything else is in SQLite |
