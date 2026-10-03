# CLAUDE.md

Guidance for contributors and coding agents working in this repo.

## What this is

Hypertracker is a shared flight board for a friend group. FastAPI + SQLite
backend, vanilla ES-module front end, no build step. Flights are resolved once
through AeroDataBox and cached; a background poller adds airborne / on ground
status from airplanes.live. Details: [README.md](README.md),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/api-contract.md](docs/api-contract.md).

## Commands

```bash
pip install -r requirements-dev.txt     # runtime + pytest + ruff
cp .env.example .env                    # set SECRET_KEY
python -m scripts.update_airports       # load airports (needs network)
python run.py                           # dev server on 127.0.0.1:8000
pytest -q -m "not e2e and not live"     # offline suite (what CI runs)
ruff check . && ruff format --check .   # lint and format check
ruff format .                           # apply formatting

pip install -r requirements-e2e.txt && playwright install chromium
pytest -m e2e                           # browser tests
python tools/contrast_check.py          # DS contrast check, when present
```

Python 3.11+. Never run `playwright install` in a restricted sandbox; CI does it.

## Architecture map

- `app/main.py`: app, middleware (CSP, headers), lifespan (starts jobs and poller), routes.
- `app/config.py`: all settings from env; validated at startup (fail fast).
- `app/validation.py`: input normalisers and validators (flight number, airport code, date).
- `app/resolver.py`: turns rows into legs with a typed status; talks to AeroDataBox.
- `app/aerodatabox.py`: upstream client and cache. `app/airplaneslive.py`: in-memory live state plus poller.
- `app/lifecycle.py`: `ends_at`, board visibility, API shaping. `app/jobs.py`: hourly purge, cache prune, refresh.
- `app/auth.py`: optional Discord OAuth. `app/netutil.py`: client IP and `TRUSTED_PROXY`.
- `app/db.py`: SQLite schema and migrations. `app/templates/`, `app/static/`: page, CSS, JS.
- `scripts/`: operational scripts. `deploy/`: systemd and nginx. `tests/`: suite and fixtures.

## Conventions

- Parse, don't validate. Pydantic models at the HTTP edge; internals take typed values.
  Malformed input is a 400, never a 500.
- Resolver outcomes are typed statuses (`ok`, `manual_ok`, `ambiguous`, `not_found`,
  `out_of_window`, `invalid`, `airport_unknown`, `upstream_unavailable`, `quota`). Do not
  collapse failures to `None`. Never cache a failure as an answer.
- No blocking sqlite in async paths. Use `asyncio.to_thread` or a sync handler.
- No network in the request path for the board. `GET /api/trips` reads cached state only.
- `ends_at` is NOT NULL and is the max over all legs: `arr_utc` if known, else end of
  `date_local` (UTC) plus 36h. One rule, in `lifecycle.py`. Board hides at `+TRIP_GRACE_HOURS`;
  hard delete at `+TRIP_PURGE_DAYS`.
- CSP forbids inline scripts and inline event handlers. Pass config through
  `<script type="application/json" id="app-config">`. Same-origin assets only; the only
  external origin is OSM tiles for images.
- URLs in the front end are relative (the page sets `<base href>`) so sub-path hosting works.
- Front end: vanilla ES modules, `textContent`/`createElement`, no hand-escaped HTML.
- Do not mention model names in code, comments, or commit text.

## Design system rules

WPTK Design System, Midnight Modern (dark default, Daylight via `data-theme="light"`).

- Use tokens only; no raw hex or px values where a token exists.
- Sentence case. No ALL CAPS labels. No emoji. No decorative icons or imagery.
- Missing components (buttons, inputs, chips, status colours, dialog) live locally in
  `app/static/css/wptk-ext.css`. The vendored DS in `app/static/wptk/` is read-only;
  update it by re-vendoring and bumping `VERSION`.
- Never edit the DS artifact itself from this repo. Propose changes to the owner
  (see `docs/DS_PROPOSAL.md`).
- Text 4.5:1 and control borders 3:1 contrast, in both themes.

## Testing rules

- Per-test DB isolation: every test gets its own database file; never share state across tests.
- Time is injectable. Pass a clock or `now` argument; do not sleep or patch the wall clock globally.
- Upstream shapes are recorded fixtures in `tests/fixtures/`, replayed with
  `httpx.MockTransport`. No test touches the network. `@pytest.mark.live` tests are
  opt-in and refresh fixtures.
- `@pytest.mark.e2e` for Playwright tests; screenshots go under `tests/e2e/**/screenshots/`.
- Use `TestClient` or `asyncio.run`; `pytest-asyncio` is not installed.
- New behaviour needs a test; bug fixes need a failing-then-passing regression test.

## Deployment constraint

Run exactly one uvicorn worker. Live state, rate limits, and the refresh budget live in
process memory, and the poller must be a single instance. Set `TRUSTED_PROXY` to match
the proxy in front. See `deploy/README.md`.

## Git

Work on a branch, keep commits focused. End commit messages with these trailers:

```
Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01M7aHfEwuWYTvsWYZo6dVGh
```

Do not commit `.env`, databases, or downloaded CSVs.
