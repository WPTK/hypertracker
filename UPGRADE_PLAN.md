# Hypertracker upgrade plan

Status: analysis complete, no code changed. Baseline: 10/10 tests pass, all offline, all manual-airport legs.

Method: I read every line of `app/*.py`, `app/static/*`, `app/templates/board.html`, `scripts/`, `deploy/`, `tests/`, config files and README. I read the WPTK Design System artifact (README, `tokens.json`, `bundle.css`, component READMEs). Three subagents did independent passes (backend, frontend with Playwright screenshots, upstream API contracts). I reproduced your two bugs and four others myself. "Repro" below means a script run against this repo.

Evidence limits: the egress proxy blocked the AeroDataBox, airplanes.live and OSM docs pages, so upstream field names and limits are **unverified**. Every such item is marked `[verify]` and the plan includes a step that records one real API response as a test fixture before relying on it.

---

## 1. Root causes of your three complaints

### 1.1 Design is outdated
`app/static/styles.css` is a verbatim copy of the `wptk-html` skill (warm black, red `#e63946`, DM Serif Display, JetBrains Mono, grain overlay, dark only). The WPTK Design System is a different language: Midnight Modern (Manrope, cobalt/orchid/teal accents, frosted translucent surfaces, 22/16/12px radii, fixed radial-gradient backdrop, Midnight + Daylight themes, sentence case, no emoji, no icons, minimal motion).

Nothing carries over except the layout skeleton. See Phase 3.

### 1.2 Flights don't auto-delete
Two independent bugs. Both reproduced.

| # | Cause | Where | Repro |
|---|---|---|---|
| A | `ends_at` is computed only from `arr_utc`, which only API-resolved legs have. Manual-airport trips and unresolved trips get `ends_at = NULL`, and NULL means "always shown, never purged". | `lifecycle.py:11-17`, `:86`, `:105-110` | Manual trip dated 2025-01-01: `ends_at=None`, still on the board, purge removed 0. |
| B | `ends_at` ignores unresolved legs in a mixed trip. A resolved outbound plus an unresolved or manual return expires 8h after the outbound lands, while the return is still in the future. | `lifecycle.py:15-17` | `compute_ends_at([{arr_utc: Sep 1}, {arr_utc: None}])` returns Sep 1. |

Compounding:
- Even when it works, deletion is "hide at +8h, hard delete at +30 days". That reads as "doesn't auto-delete".
- The purge loop swallows every exception (`main.py:25`), runs sync SQLite on the event loop, and runs once per 24h.
- `flight_cache`, `aircraft_cache` and `users` are never pruned.
- Browser `localStorage` keeps manage tokens for trips the server already deleted.
- The tests hide this: they insert manual trips dated 2026-07-01 (already past) and assert nothing about their disappearance.

### 1.3 Flights struggle to validate
There is no validation layer. Input goes from the browser straight into an upstream URL path.

| # | Cause | Where | Repro |
|---|---|---|---|
| 1 | No format check on flight number, date, or airport codes. Empty date sends `/flights/number/DL1200/`. A crafted flight number rewrites the upstream path (`/flights/number/../../aircrafts/reg/N1/...`). | `main.py:242-248`, `aerodatabox.py:53` | Yes |
| 2 | `_pick_flight` accepts a flight from a **different date** (`pool = [...] or list(flights)`) and ignores the user's From/To when only one candidate exists. With several candidates and no hint it takes the first in API order. The result is `resolved=1` with the wrong flight. | `aerodatabox.py:87-101` | Yes |
| 3 | All upstream failures collapse to `None`: bad key (401), quota (429), 5xx, timeout, "no such flight" (likely 204 `[verify]`). Nothing is logged and the error body is discarded. The UI says "check the flight number and date" for all of them. | `aerodatabox.py:28-37` | Yes |
| 4 | A 200 with a junk body (`{"message":"quota"}`) is wrapped as `[data]`, cached 30 days, and stored as `resolved=1` with no airports or times. | `aerodatabox.py:61-67` | Yes (backend agent) |
| 5 | Future flights are cached 30 days. Tail numbers are usually assigned close to departure `[verify]`, so a trip entered 3 weeks out is frozen with no aircraft type or age. | `config.py:65` | By reading |
| 6 | Save returns 200 even when every leg is unresolved. The user finds out afterwards, in the board, as "couldn't resolve". There is no preview and no per-row feedback. | `main.py:345-354`, `app.js:464-515` | Yes |
| 7 | Legs resolve one at a time, each up to 15s + 15s, no overall deadline. 8 legs can take 4 minutes, past the proxy timeout, so users retry and double-post. | `main.py:253-258` | By reading |
| 8 | Typo'd airports are accepted (`ZZZ` to `YYY` returns 200 with null airports), producing a ghost trip with `ends_at=NULL`, shown forever. | `resolver.py:80-95` | Yes (backend agent) |
| 9 | `ac_type` likely never fills: the code reads `typeCode`/`icaoCode`, which probably don't exist on the aircraft record (`modelCode`, `ageYears` likely do) `[verify]`. | `resolver.py:136` | Needs live record |
| 10 | Malformed bodies raise 500s (`[]`, invalid JSON, non-string fields, huge `trip_id`). `tz` is stored unclipped. | `main.py:319-320` | Yes |

---

## 2. Other findings, ranked

### Critical / high
1. **Default `SECRET_KEY`** (`config.py:18`): forging a session cookie for an admin Discord ID returned `is_admin: true`. Repro. Also makes `public_owner_id` hashes brute-forceable.
2. **Rate limit bypass** (`main.py:73-80`): the first `X-Forwarded-For` value is client-controlled behind the nginx example. Six spoofed requests all returned 200. Each create can spend up to 16 paid API calls. Repro.
3. **Sync SQLite inside async handlers**: a held write lock froze the whole event loop for exactly 5.0s, then 500'd (backend agent repro).
4. **`/api/trips` probes airplanes.live inside the request**, behind one global lock that is held while sleeping: 6 cold callsigns took 5.3s. Any visitor can trigger it. A 429 is cached as "not airborne" for 60s. A parked aircraft with a live transponder counts as airborne.
5. **Accessibility**: clickable airport and flight tokens are `<span>`s (keyboard users cannot filter at all). The modal has no dialog role, Escape handling or focus trap. `--text-faint` is 2.43:1. Filtered-out rows are about 1.5 to 2.2:1.
6. **A failed 90s poll replaces the whole board with "Could not load the board."**
7. **Map breaks silently if Leaflet fails to load**; the map jumps back to the global fit every 90s.
8. **`DEV_MODE` is a warning, not a guard**: every visitor is admin if it leaks into production.

### Medium
- `?manage=<trip>.<token>` puts a credential in the query string: it lands in nginx and Cloudflare logs. Use a URL fragment.
- Browser identity proof reuses a trip's manage token (`keys[-1]`). Once that trip is deleted or purged, proof fails silently and the person's trips split into new name groups. Needs a dedicated identity secret.
- `edit_trip` race: a trip deleted or purged during slow resolution gives an `IntegrityError` 500. Concurrent edits are last-write-wins.
- `_preserve_resolved` skips manual legs, so an edit during an API outage can replace resolved data with a bare route.
- OAuth: `/users/@me` and `/guilds` responses are never status-checked, so a Discord 429 gives a 500 (`KeyError: 'id'`). `/login` redirects with an empty client id. `prompt=consent` on every login. The `guilds` scope is requested even when no guild gate is configured. `/logout` is a GET.
- `update_airports` deletes the table before parsing: a bad CSV wiped 2 rows to 0 (repro). Rows with an empty ident collide. IATA lookup is nondeterministic on duplicates.
- OSM tiles: the usage policy prohibits heavy use and requires a valid Referer and User-Agent `[verify via policy page]`. A public board risks a block. README still claims CARTO.
- No CSP. No `Referrer-Policy` on the page itself for the pre-`replaceState` window.
- Edit form silently discards typed values when you toggle manual mode. A mixed trip opens in an inconsistent state.
- Invalid rows are silently dropped by `gather()`; the server's error body is never shown; Save is not disabled during the request (double-post).
- Delete and edit failures are silent; native `confirm()` doesn't say which trip.
- Date column wraps (`2026-10-\n10`) on desktop. Touch targets are 20px high. `.hint` disappears on mobile.
- Empty state skips map and chip updates; stale filter dims everything with no message.
- API returns data the UI never shows: arrival time, airport names, IATA codes, aircraft type, build date, resolved/manual flags. Dead code: `planeIc`, `.ic`, `.btn--live`, `.btn--muted`, `.man-toggle`, `.ap-label`, `live_url`, `globe_url`.
- Manual legs render a bare "—" and a useless FlightAware link to `/live/flight/` (no ident). `fa_url` ident is not URL-encoded.
- Tests cover 0% of the resolver, `_pick_flight`, cache behaviour, mixed-leg lifecycle, OAuth success path, edit with resolved legs, malformed input, or the frontend. One test uses `pytest.raises(Exception)`. No CI, no lint, no type check, dependencies unpinned.

### Low
Cache tables unpruned; `_age_years` can go negative; raw flight number used as callsign; airport search has no length cap, wildcard escaping, or rate limit; `journal_mode=WAL` set before `busy_timeout`; systemd and nginx examples lack hardening and `limit_req`; `esc()` doesn't escape `'` (safe today, latent); the noise overlay sits above the modal; README is stale (`adsb.cc`, CARTO, "Live link", "your adsb.cc design system").

---

## 3. Target architecture

Keep the stack (FastAPI, SQLite, vanilla JS, no build step). It fits a single-VPS hobby board and the DS is plain CSS. Change the structure inside it.

```
app/
  main.py            app factory, middleware, lifespan only
  config.py          settings; validated at startup (fail fast)
  schemas.py         pydantic request/response models
  routers/ pages.py auth.py trips.py airports.py
  services/ trips.py (CRUD + authz)  resolver.py  lifecycle.py  identity.py
  clients/ aerodatabox.py  airplaneslive.py       (one shared httpx.AsyncClient)
  db.py              connection helper + numbered migrations (PRAGMA user_version)
  jobs.py            hourly purge, cache prune, live-status poller, near-term refresh
  static/ wptk/ (vendored DS), js/ (ES modules), fonts/
tests/ unit/ api/ fixtures/ (recorded API responses)
```

Principles:
- **Parse, don't validate.** Pydantic models at the edge; internals receive typed values.
- **One lifecycle rule, written once.** `ends_at` is `NOT NULL`. Every consumer reads the same column.
- **Failures are typed.** `Resolved | NotFound | Ambiguous | OutOfWindow | UpstreamDown | QuotaExceeded | BadKey`. The UI shows the actual state.
- **No network or blocking DB work in the request path** for the board. Live status comes from a cache a background task fills.
- **Every upstream shape is a recorded fixture** with a contract test.

---

## 4. Phases

Estimates are my working time including tests. Each phase ships on its own.

### Phase 0: Safety net (about 3 hours)
1. Refuse to start when `SECRET_KEY` is unset or the default (unless `DEV_MODE` on localhost). Use a separate derived key for `public_owner_id`.
2. `DEV_MODE` only honoured when `BASE_URL` host is localhost/127.0.0.1; otherwise abort.
3. Client IP: use the TCP peer by default. Add `TRUSTED_PROXY=cloudflare|nginx|none`. For nginx take the right-most untrusted XFF hop; for Cloudflare take `CF-Connecting-IP` only when the peer is localhost. Fix `deploy/nginx.conf.example` to overwrite the headers. Add a global write cap.
4. Pydantic models for every request body; invalid JSON returns 400; `trip_id` bounds; `tz` validated against `zoneinfo`.
5. Add GitHub Actions: `ruff`, `pytest`, pinned `requirements.txt` (plus lock file).
6. Replace the swallow-all `except: pass` in housekeeping with `logging.exception`.

Exit check: forged-cookie, XFF-spoof and malformed-body repros become regression tests that now pass.

### Phase 1: Lifecycle, so flights really auto-delete (about 4 hours)
1. Make `ends_at` `NOT NULL`. Per leg: `arr_utc` if present, else the end of `date_local` plus 36h (covers any timezone and overnight flights). Trip `ends_at` is the max over **all** legs.
2. Require a date on every leg (client and server).
3. Migration (v2): backfill existing NULLs using leg dates, falling back to `created_at + 3 days`; add the `NOT NULL` default behaviour; add `CHECK` via recreate if desired.
4. Retention, one clear rule with two config values:
   - Board visibility: until `ends_at + BOARD_GRACE_HOURS` (default 8, unchanged).
   - Hard delete: `ends_at + RETENTION_DAYS` (default **3**, down from 30; your call, see section 6).
5. `jobs.py`: hourly purge in `asyncio.to_thread`, logs `purged N trips, M cache rows`, prunes `flight_cache`, `aircraft_cache`, orphan `users`. Runs once at startup.
6. Early end via status: when a leg's status is Landed or Canceled `[verify field names]`, end it at landing/cancellation, not schedule.
7. Client: drop stored manage tokens that the server reports as 404; drop tokens for trips absent from `/api/trips` after the next successful load.
8. Tests with an injectable clock: manual-only, mixed, unresolved, overnight, timezone edge, grace boundary, purge boundary, migration backfill, prune.

Exit check: the 2025-01-01 manual trip is gone from the board and the DB after one purge run; the mixed-leg trip stays until its return lands.

### Phase 2: Validation and resolution (about 1.5 days)
1. **Input rules** (server authority, client mirrors): flight number `^[A-Z0-9]{2}\d{1,4}[A-Z]?$` (IATA, at least one letter in the first two) or `^[A-Z]{3}\d{1,4}[A-Z]?$` (ICAO) after normalising, strip leading zeros `[verify]`; date ISO, within a configurable window (`today - 2d` to `today + FLIGHT_WINDOW_DAYS`, default 330 `[verify]`); airport codes `^[A-Z0-9]{3,4}$` and must exist in `airports`.
2. **Typed resolver result** (see section 3). `_pick_flight` rewrite: filter by date (no fallback to other dates), apply hints to single candidates too, sort by departure, return `Ambiguous(candidates)` instead of guessing.
3. **Upstream client**: one shared `httpx.AsyncClient`; split 204 / 401 / 403 / 429 / 5xx / timeout; log status plus truncated body; one retry on timeout or 5xx with jitter; circuit breaker on 401 and 429 so we stop burning quota; configurable auth-header scheme (RapidAPI vs API.Market `[verify]`); schema-check the payload before caching (reject junk, reject `resolved` without airports/times); `quote()` every path segment.
4. **Caching**: completed flights 30d; future flights short TTL (hours) and a negative cache (5 min) for not-found; never cache failures or reg-less near-term flights beyond a day.
5. **Preview endpoint** `POST /api/legs/preview`: takes the rows, returns per-row `{status, leg?, candidates?, message}`. Parallel with a semaphore and a 20s overall deadline; separate lightweight rate limit; cache-backed so previewing then saving costs one API call.
6. **Save semantics**: server re-resolves from cache (never trusts client-supplied leg data). A flight leg that is not `Resolved` is rejected with a specific message unless the client sends `accept_unverified: true`, which stores it with a visible "unverified" badge. Manual airports must exist.
7. **Aircraft**: fetch one real `/aircrafts/reg/` record, store as fixture; use `modelCode` and `ageYears` if confirmed `[verify]`; clamp negative ages.
8. **Near-term refresh job**: for active trips, re-resolve future legs once at T-48h and T-24h and again at T-3h (3 calls per leg total). This fills tail, age and type when airlines assign them, and picks up delays and cancellations. Budget capped by `MAX_REFRESH_PER_DAY`.
9. **Edit race**: re-check the row inside the write transaction (404 if gone); `updated_at` optimistic check; `_preserve_resolved` also preserves for manual legs when the flight and date are unchanged.
10. **Live status**: background poller (one task, 1 req/s) for callsigns in the departure-20min to arrival+45min window; `/api/trips` reads the in-memory result. Airborne means numeric `alt_baro` above 0 `[verify]`, not `"ground"`. Failures are not cached as "false"; show "unknown", not "not airborne".
11. `scripts/update_airports.py`: parse first, abort below a row-count floor, swap in one transaction, skip empty idents, `ORDER BY` on IATA lookup.
12. Tests: recorded fixtures (single leg, multi-leg same number, wrong date, 204, 429, 401, junk 200, null body), `httpx.MockTransport`, property tests on the normaliser, pick-flight table tests.

Exit check: each of the ten causes in 1.3 has a failing-then-passing test.

### Phase 3: Design system migration and frontend rewrite (about 3 days)

**3a. Vendor the Design System** (I will not link to the live artifact at runtime).
- Copy `tokens.css` (generated from `tokens.json`; do **not** use `colors_and_type.css`, which is stale: old `--line`, `maxw 1280px`), the Manrope variable font, and a trimmed subset of `bundle.css` into `app/static/wptk/`. Keep only what the board uses: base/body gradient, nav + theme toggle, `.surface`, `.hub-card`, `.wptk-section`, `.skip-link`, footer. Drop the page-specific CFP/FBS/nooclear/350 rules and the `!important` legacy helpers.
- Record the DS version/hash in `app/static/wptk/VERSION` so drift is visible.
- Do **not** pull in the React bundle. The components are re-created as plain HTML/CSS from their READMEs.
- Self-host fonts; remove the Google Fonts dependency.

**3b. Mapping (old to new)**

| Element | Now | New (DS) |
|---|---|---|
| Page | flat `#0e0e0e` + noise | `body.wptk` fixed radial gradient, Midnight / Daylight, toggle persisted in `localStorage` |
| Header | red serif logo + Zulu clock | `wptk-nav` breadcrumb ("WPTK / Hypertracker"), built-in theme toggle, burger below 900px; clock moves into the page header |
| Person card (`.flyer`) | bordered box, red hover border | `SectionCard` (`surface`, `radius-lg`, accent bar cycling cobalt/orchid/teal per person) |
| Controls | segmented control, red buttons | pill controls (`border-radius:999px`, cobalt tint when active) |
| Trip detail modal | custom div | native `<dialog>` on `surface`, `shadow` (reserved elevation, the DS says use it for modals) |
| Empty / hint cards | italic grey text | `hub-card` style |
| Tokens (airport/flight) | dashed-underline span | `<button class="tok">` |
| Type | DM Serif / Source Sans / JetBrains Mono | Manrope; DS `mono` stack for codes |

**3c. Gaps in the DS that the board needs.** The DS has no form controls, buttons, badges/status chips, table/list rows, tooltips or dialog. Proposed additions, tested for 4.5:1 text and 3:1 control borders in both themes:
- Semantic status colours (airborne, delayed, cancelled, landed, unverified). The DS has only three accents, which must also be told apart by lightness, not hue alone.
- `data` text styles (the DS body is 19px, too large for dense rows; `hub-desc` 13px is too small for times).
- Input, button (primary/ghost), chip, dialog.
I would build these locally in `app/static/wptk-ext.css` first, then offer them back to the DS. I have **not** modified the DS artifact.

**3d. Copy.** DS voice: first person, sentence case, no ALL CAPS, no emoji, deadpan. Drop `text-transform:uppercase` labels. Examples: "Add a trip", "Lands 10:05 in Denver", "Couldn't find that flight on that date. Check the date, or add the airports yourself."

**3e. Information architecture**
- Row shows: day group (Today / Tomorrow / Sat Oct 10), departure and **arrival** local times with zone abbreviation, IATA primary with city name, aircraft type, age, tail, status chip (scheduled, airborne, landed, delayed, cancelled, unverified), "lands in 3h".
- An "In the air now" strip at the top.
- Last-updated stamp and a stale banner when polling fails (keep old data, never blank the board).
- Skeleton loading; real empty state with the add action.
- Hide the FlightAware link when there is no ident.

**3f. Frontend architecture** (no build step)
- ES modules: `api.js`, `state.js`, `board.js`, `map.js`, `tripForm.js`, `a11y.js`. `<template>` elements and `textContent`/`createElement`, so there is no hand-escaped HTML. Event delegation instead of `.onclick` rebinding.
- Diff-render: skip the DOM update when the payload hash is unchanged; pause polling when the tab is hidden or the dialog is open; use `ETag`/`If-None-Match` so a poll is usually a 304.
- `load()` with `AbortController` and an in-flight guard.
- Trip form: per-row inline validation using the preview endpoint (a "verified" card appears under each row as soon as it resolves; ambiguous numbers show a pick-the-leg list instead of From/To guessing). Save disabled while busy. Server error messages shown verbatim. Toggling manual mode converts rows instead of discarding them.
- Destructive confirm: custom dialog naming the trip; busy state; 404 treated as already removed.
- Manage link: URL fragment `#manage=` (never sent to servers), confirmation toast, validate ids, add `<meta name="referrer">`.

**3g. Accessibility acceptance**
- Everything operable by keyboard; `:focus-visible` ring using `accent-0`; `aria-pressed` on tokens and view toggle; `role="status"` live regions for filter, save and error text; labelled inputs with `aria-invalid`/`aria-describedby`; 44px touch targets on mobile; `prefers-reduced-motion` honoured; contrast checked in both themes by a script in CI; Playwright + axe run in CI.

**3h. Map** (needs your decision, section 6). Recommendation: **no tile server at all.** Draw a simplified Natural Earth land outline (public domain, a few hundred KB or less) with great-circle arcs in SVG/canvas, themed from DS tokens in both Midnight and Daylight. The DS says "Imagery: none", this removes the OSM policy risk and the CDN failure mode, and the raster-invert hack in `styles.css:94`. Cost: no street-level zoom, which this board never needed. Alternatives: MapLibre + OpenFreeMap (no key, vector, needs a custom dark style) or Leaflet + CARTO (check current key/terms `[verify]`).

Exit check: Lighthouse a11y of at least 95, axe zero serious, both themes screenshot-diffed at 390px and 1280px.

### Phase 4: Identity, hardening and docs (about 1 day)
1. Identity: `identities(id, secret_hash)` table. Browser holds one secret; proof is that secret, not a trip token. Existing m_ owners migrate by claiming through their existing trip tokens once.
2. CSP with a nonce (inline `__APP__` script moves to a JSON `<script type=application/json>` block); `Referrer-Policy`, `Permissions-Policy`.
3. OAuth: status-check Discord calls; clean error when unconfigured; drop `prompt=consent`; request `guilds` only when `DISCORD_GUILD_ID` is set; `/logout` as POST; explicit session `max_age`.
4. Airport search: length cap, escape `%`/`_`, rate limit, prefix-first query with an index.
5. `deploy/`: systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`), documented single-worker constraint (in-process state), nginx `limit_req`, `client_max_body_size`, correct real-IP handling, `--proxy-headers` guidance.
6. README rewrite to match reality (no `adsb.cc` or CARTO claims), architecture diagram, retention rules, `.env.example` updated.
7. Add `CLAUDE.md` with run/test/lint commands and the conventions above.

---

## 5. Test strategy
- Unit: normaliser, validators, `_pick_flight`, lifecycle math with an injected clock, age maths, cache-poisoning guards.
- API: every endpoint with valid, malformed and hostile bodies; authz matrix (anon, token, owner, admin, wrong token); rate limit with spoofed headers; OAuth success, state mismatch, Discord error bodies; migration from a v1 database fixture.
- Contract: recorded upstream fixtures replayed through `httpx.MockTransport`. A manual, opt-in `pytest -m live` job refreshes the fixtures.
- Frontend: Playwright for add, edit, remove, filter, keyboard-only flow, failed poll, map fallback; axe in both themes.
- Isolation: per-test DB and per-test rate-limit state (today the suite shares both).

---

## 6. Decisions I need from you

1. **Retention.** Hide at +8h after landing (unchanged) and hard-delete at +3 days (my recommendation; currently 30). Or hard-delete at +8h?
2. **Map.** Tile-less SVG world map (recommended), MapLibre + OpenFreeMap, or Leaflet + CARTO?
3. **Missing dates.** Make a date mandatory on every leg (recommended, it removes the NULL class of bug)?
4. **DS additions.** Build the missing form/status/dialog components locally and later propose them to the DS (recommended), or have me add them to the DS artifact first?

## 7. Order and cost
Phase 0 (3h) to Phase 1 (4h) to Phase 2 (1.5d) to Phase 3 (3d) to Phase 4 (1d). Phases 0 to 2 fix all three of your complaints' root causes and the security issues before any visual work starts; Phase 3 is the redesign. About 6 working days in total.
