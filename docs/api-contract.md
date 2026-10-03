# Hypertracker API and module contract

Single source of truth for the parallel upgrade work. If you need to change it, say so in your final report; do not silently diverge.

## Decisions (from the owner)
- Retention: board hides a trip at `ends_at + BOARD_GRACE_HOURS` (8h). Hard delete at `ends_at + RETENTION_DAYS` (3 days). Env names: `TRIP_GRACE_HOURS` (existing), `TRIP_PURGE_DAYS` (existing, default becomes 3).
- `ends_at` is NOT NULL. Per leg: `arr_utc` if known, else end of `date_local` (UTC) + 36h. Trip `ends_at` = max over ALL legs.
- Date is optional in the UI/API: a missing date defaults to today (UTC date) via `app.validation.validate_leg_date`. Adding a flight must never fail for lack of a date.
- Map: keep Leaflet + OpenStreetMap tiles (small friends-only board; owner accepts the usage). Vendor Leaflet locally, fail gracefully, theme-aware. MapLibre only if Leaflet cannot work.
- Airborne check stays, plus an explicit on-ground state.
- Design system: WPTK Design System (Midnight Modern). Gaps (buttons, inputs, chips, status colours, dialog) are built LOCALLY in the app and proposed to the owner; the DS artifact is NOT modified.
- No new build step. Vanilla ES modules. Python 3.11, FastAPI, SQLite, pytest.

## Shared helpers (already written, do not re-implement)
`app/validation.py`: `normalize_flight_no`, `is_valid_flight_no`, `normalize_airport_code`, `validate_leg_date(raw, today, past_days, future_days) -> (iso|None, error|None)`.

## HTTP API

### GET /api/trips
Supports `ETag` / `If-None-Match` (304 when unchanged; ETag covers trips + live states).
```json
{
  "trips": [{
    "id": 12, "owner_id": "<hashed>", "owner_name": "Alex",
    "out": [Leg], "ret": [Leg]
  }],
  "me": "<hashed owner id>|null", "is_admin": false,
  "server_time": 1790000000,
  "live_updated_at": 1790000000
}
```
Leg (all keys always present, value may be null):
`direction, seq, date_local, flight_no, callsign,
from (ICAO), from_iata, from_name, from_city, from_lat, from_lon,
to, to_iata, to_name, to_city, to_lat, to_lon,
dep_local, arr_local, dep_utc, arr_utc,
reg, ac_type, ac_model, ac_age, ac_built,
resolved (bool), manual (bool), unverified (bool),
live_state ("airborne" | "on_ground" | null), fa_url (string | null)`.
- `dep_local`/`arr_local`/`dep_utc`/`arr_utc` are the AeroDataBox strings, e.g. `"2026-10-10 08:15-04:00"` / `"2026-10-10 12:15Z"`.
- `unverified` = a flight-number leg the user chose to keep although it could not be resolved.
- `fa_url` is null when there is no flight number or callsign. `live_url` is removed.

### POST /api/legs/preview  (new; rate limited more loosely than writes; no DB writes)
Request: `{"rows": [{"kind": "flight"|"manual", "flight_no"?: str, "date"?: str, "from"?: str, "to"?: str}]}` (max `MAX_LEGS_PER_TRIP * 2` rows).
Response: `{"results": [{"index": 0, "status": Status, "message": str, "leg": Leg|null, "candidates": [Candidate]}]}`.
`Status` is one of: `ok`, `manual_ok`, `ambiguous`, `not_found`, `out_of_window`, `invalid`, `airport_unknown`, `upstream_unavailable`, `quota`.
`Candidate` = `{"from": icao, "from_iata": str|null, "to": icao, "to_iata": str|null, "dep_local": str|null, "arr_local": str|null}`; for `ambiguous`, the client re-previews with `from`/`to` set to pick one.
`message` is plain, first-person-friendly, sentence case, safe to show verbatim.

### POST /api/trips   and   PUT /api/trips/{id}
Body: `{"name"?: str, "uid"?: str, "proof"?: str, "tz"?: str, "out": [Row], "ret": [Row], "accept_unverified"?: bool}`, `Row = {"flight_no"?: str, "date"?: str, "from"?: str, "to"?: str}`.
A row with `flight_no` is a flight row (from/to are optional hints); a row without it needs both `from` and `to` (manual).
- A row whose status is not `ok`/`manual_ok` makes the request fail with 400 unless `accept_unverified` is true AND the row status is one of `not_found`, `upstream_unavailable`, `quota` (stored with `unverified: true`; `invalid`, `out_of_window`, `airport_unknown`, `ambiguous` always fail).
- Success: `{"ok": true, "trip_id": int}` plus, for anonymous creates, `manage_token` and `uid` (unchanged behaviour).

### Errors (all 4xx/5xx JSON)
`{"detail": "message"}` or `{"detail": {"message": str, "rows": [{"direction": "out"|"ret", "index": int, "status": Status, "message": str}]}}`.
Malformed bodies are 400 (never 500). Pydantic validation errors are converted to this shape (400, not 422).

## Python interfaces between backend modules

`app/resolver.py` (owner: resolution agent)
- `async resolve_rows(rows: list[dict], *, deadline: float = 20.0) -> list[dict]`
  Input row: `{"direction": "out"|"ret", "seq": int, "flight_no": str, "date": "YYYY-MM-DD", "from": str|None, "to": str|None}` (already validated/normalised by the caller).
  Output: one dict per input row = the DB leg columns (`trip_id` excluded) plus `status`, `message`, `candidates`, `unverified` (bool). Rows resolve concurrently (semaphore), bounded by `deadline`.
- `async refresh_leg(leg: dict) -> dict | None`: re-resolve bypassing the cache; returns the changed DB columns or None.
- `normalize_flight_no` re-exported from `app.validation`.

`app/airplaneslive.py` (owner: resolution agent)
- `get_state(callsign: str | None) -> "airborne" | "on_ground" | None`: pure in-memory read, no network, never blocks.
- `async run_poller(get_callsigns: Callable[[], Iterable[str]], interval: float = 60.0) -> None`: loops forever; probes each callsign sequentially with >= 1.0s spacing; on API failure keeps the last known state (does NOT write False). Cancellation-safe.
- `globe_url` stays; unused by the API.

`app/lifecycle.py` / `app/jobs.py` (owner: backend core agent)
- `compute_ends_at(legs: list[dict]) -> int` never returns None.
- `shape_trip` reads `airplaneslive.get_state` (no network in the request).
- `jobs.run_housekeeping_loop()`: hourly purge + cache prune via `asyncio.to_thread`, logged. Lifespan also starts `airplaneslive.run_poller` with a callsign supplier (callsigns of legs in the window from departure-20min to arrival+45min).
- `jobs.refresh_upcoming()`: calls `resolver.refresh_leg` for legs departing within 48h that lack a registration (budget `MAX_REFRESH_PER_DAY`, default 40).

## Frontend module layout (static, ES modules, no build)
```
app/static/
  css/app.css        (shell, board, layout; imports wptk)       owner: shell agent
  css/wptk-ext.css   (local DS extensions: buttons, inputs, chips, status, dialog)  owner: shell agent
  css/map.css        owner: map agent
  css/form.css       owner: form agent
  wptk/              vendored DS: tokens.css, bundle subset, VERSION, fonts/   owner: shell agent
  vendor/leaflet/    vendored Leaflet 1.9.4                      owner: map agent
  js/main.js         entry (type=module): state, polling, board render, filters, theme, nav wiring   owner: shell agent
  js/api.js          owner: shell agent
  js/identity.js     owner: form agent
  js/map.js          owner: map agent
  js/tripForm.js     owner: form agent
```
`js/api.js` exports: `request(method, url, body?, {manageToken?, signal?}) -> Promise<{ok, status, data}>` (never throws on HTTP errors; throws only on network failure as `{ok:false,status:0,data:null,networkError:true}` returned, not thrown); `fetchTrips({etag, signal}) -> Promise<{notModified?:true, etag, data}>`. All URLs are relative (`api/trips`) because the page uses `<base href>`.

`js/identity.js` exports: `identity()`, `saveIdentity(uid, name)`, `manageToken(tripId)`, `rememberManage(tripId, token)`, `forgetManage(tripId)`, `pruneManage(liveTripIds:Set)`, `canManage(ownerId, tripId, {me, isAdmin})`, `proofToken()`, `seedFromFragment()` (reads `#manage=<trip>.<token>`, validates `^\d+$`, stores, strips the fragment, returns {tripId}|null).

`js/map.js` exports: `createMap(containerEl, {onSelectCode(code)}) -> {update(trips, filter), setTheme('dark'|'light'), show(), hide(), destroy()}`. `filter` = `{tok: string|null, date: string|null}`. Handles missing Leaflet / tile failure with a visible message and never throws.

`js/tripForm.js` exports: `createTripForm({user, getTrips, onSaved()}) -> {openNew(), openEdit(tripId), openConfirmRemove(tripId)}`. It creates and owns its own `<dialog>` elements (appended to `document.body`). `user` = `{id,name}|null`.

`js/main.js` wires them: a `#openAdd` button calls `openNew()`; each trip's edit/remove buttons call `openEdit(id)` / `openConfirmRemove(id)`.

## CSS class API (defined by the shell agent in wptk-ext.css; the others may use them)
`.btn`, `.btn--primary`, `.btn--ghost`, `.btn--danger`, `.btn--sm`, `.input` (text/date/select), `.field`, `.field__label`, `.field__hint`, `.field__error`, `.chip` (+ `.is-active`), `.status` with modifiers `.status--airborne`, `.status--ground`, `.status--scheduled`, `.status--landed`, `.status--delayed`, `.status--cancelled`, `.status--unverified`, `.dialog` (on `<dialog>`), `.sr-only`, `.surface`, `.is-hidden`. Themes: `<html data-theme="light">` for Daylight; absent/`dark` is Midnight (DS convention). Body class `wptk`.

## Quality bar for every agent
- Tests: add/extend pytest; whole suite must pass: `cd <worktree> && python -m pytest -q`.
- Commit in your worktree branch with message ending in these two trailers:
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01M7aHfEwuWYTvsWYZo6dVGh`.
- Do NOT push, do NOT open PRs, do NOT edit files owned by another agent. Report what you changed and anything the contract needs.
- Do not mention model names in code, comments or commit text.

## Addendum: shared knobs, template and security contract
- `app/netutil.py: client_ip(request)` exists (peer address). Backend core extends it with trusted-proxy handling; everyone else only calls it.
- New config names (backend core adds them to `config.py` with exactly these defaults; the resolution agent reads them with `getattr(config, NAME, default)`): `FLIGHT_CACHE_TTL_PAST=2592000`, `FLIGHT_CACHE_TTL_NEAR=21600`, `FLIGHT_NEGATIVE_TTL=600`, `FLIGHT_WINDOW_PAST_DAYS=2`, `FLIGHT_WINDOW_FUTURE_DAYS=330`, `UPSTREAM_TIMEOUT=8`, `RESOLVE_DEADLINE=20`, `RESOLVE_CONCURRENCY=3`, `AERODATABOX_AUTH=rapidapi` (`rapidapi`|`apimarket`), `PREVIEW_RATE_LIMIT=60`, `PREVIEW_RATE_WINDOW=600`, `MAX_REFRESH_PER_DAY=40`, `TRUSTED_PROXY=none` (`none`|`cloudflare`|`nginx`), `TRIP_PURGE_DAYS=3`.
- Template/CSP: no inline executable scripts and no inline event handlers. The page passes config via `<script type="application/json" id="app-config">` (keys: `user`, `isAdmin`, `discordEnabled`, `gated`, `basePath`). Template context from `main.py` stays: `user, is_admin, discord_enabled, gated, base_path`. Only same-origin scripts/styles/fonts; images may also load from `https://*.tile.openstreetmap.org`. Backend core sets `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.tile.openstreetmap.org; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com`.
- Logout is `POST logout` (form post, 303 back to the board). Login stays `GET login`; when Discord is not configured `GET login` redirects to `./?auth=unavailable`.
- Auth module (`app/auth.py`, owner: auth agent): adds `DiscordError(Exception)`, `is_configured() -> bool`; `exchange_code(code)` raises `DiscordError` on any non-2xx / non-dict / missing-id response; `login_url(state)` requests scope `identify guilds` only when `DISCORD_GUILD_ID` is set (else `identify`) and uses `prompt=none`; guild list is paged so >200 guilds work.
