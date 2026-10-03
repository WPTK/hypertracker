#!/usr/bin/env bash
# One-command deploy for Hypertracker on the host that runs it.
#
#   sudo APP_DIR=/opt/hyperfixed-flight-tracker SMOKE_FLIGHT=DL1200 ./deploy/deploy.sh
#
# Steps, in order. Any failure stops the script before the service is touched,
# except the smoke test, which runs after the restart and exits non-zero on failure.
#   1. back up the SQLite database (SQLite's own backup, so WAL is safe) and verify it
#   2. .env: create from .env.example if missing, generate SECRET_KEY only when it is
#      missing or still a placeholder, set TRUSTED_PROXY=cloudflare, DEV_MODE=false.
#      Existing AERODATABOX_* values are never touched.
#   3. install dependencies into the virtualenv
#   4. load the airport table (keeps the old table if the download is truncated)
#   5. refuse to continue if the systemd unit starts more than one worker
#   6. restart the service and wait for it to answer
#   7. smoke test: page headers, board API, and (with SMOKE_FLIGHT) one real lookup
#
# Secrets are never printed. Override any variable below from the environment.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/hyperfixed-flight-tracker}"
SERVICE="${SERVICE:-hyperfixed-web}"
BASE="${BASE:-http://127.0.0.1:8000}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
RESTART_CMD="${RESTART_CMD:-systemctl restart $SERVICE}"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/$SERVICE.service}"
TRUSTED_PROXY_VALUE="${TRUSTED_PROXY_VALUE:-cloudflare}"
SMOKE_FLIGHT="${SMOKE_FLIGHT:-}"      # e.g. DL1200; empty skips the upstream lookup
SMOKE_DAYS="${SMOKE_DAYS:-3}"         # how many days ahead to look the flight up
AIRPORTS_FILE="${AIRPORTS_FILE:-}"    # load from a local CSV instead of downloading
AIRPORTS_MIN_ROWS="${AIRPORTS_MIN_ROWS:-5000}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
SKIP_AIRPORTS="${SKIP_AIRPORTS:-0}"
SKIP_SMOKE="${SKIP_SMOKE:-0}"
SKIP_RESTART="${SKIP_RESTART:-0}"     # for tests and dry runs: do not restart or wait

ENV_FILE="$APP_DIR/.env"
say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -d "$APP_DIR" ] || die "APP_DIR $APP_DIR does not exist"
cd "$APP_DIR"
if [ ! -x "$PYTHON" ]; then
  if [ "$SKIP_INSTALL" = 1 ] || [ -n "${PYTHON_FALLBACK:-}" ]; then PYTHON="${PYTHON_FALLBACK:-python3}"; else
    say "Creating virtualenv"; python3 -m venv .venv
  fi
fi

# ---------------------------------------------------------------- .env
say "Configuration (.env)"
[ -f "$ENV_FILE" ] || { [ -f .env.example ] || die ".env.example is missing"; cp .env.example "$ENV_FILE"; echo "created .env from .env.example"; }
chmod 600 "$ENV_FILE"
"$PYTHON" - "$ENV_FILE" "$TRUSTED_PROXY_VALUE" <<'PY'
import re, secrets, sys
path, proxy = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
PLACEHOLDERS = {"", "change-me-in-production", "change-me-to-a-long-random-string"}

def get(key):
    for ln in lines:
        m = re.match(rf"^\s*{key}\s*=\s*(.*?)\s*(#.*)?$", ln)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None

def put(key, value):
    for i, ln in enumerate(lines):
        if re.match(rf"^\s*{key}\s*=", ln):
            lines[i] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")

if get("SECRET_KEY") in PLACEHOLDERS or len(get("SECRET_KEY") or "") < 32:
    put("SECRET_KEY", secrets.token_urlsafe(48))
    print("SECRET_KEY: generated a new one (48 bytes, not shown)")
else:
    print("SECRET_KEY: already set, left alone")
put("TRUSTED_PROXY", proxy);  print(f"TRUSTED_PROXY: {proxy}")
put("DEV_MODE", "false");     print("DEV_MODE: false")
open(path, "w").write("\n".join(lines) + "\n")
print("AERODATABOX_KEY: " + ("set, left alone" if get("AERODATABOX_KEY") else "EMPTY: flights will not resolve until you set it"))
print("AERODATABOX_AUTH: " + (get("AERODATABOX_AUTH") or "rapidapi (default)"))
PY

# Values the app itself will use (DB path comes from .env if set).
DB_PATH_VALUE="$("$PYTHON" - "$ENV_FILE" "$APP_DIR" <<'PY'
import re, sys
env, app = sys.argv[1], sys.argv[2]
for ln in open(env):
    m = re.match(r"^\s*DB_PATH\s*=\s*(.*?)\s*$", ln)
    if m and m.group(1).strip('"\''):
        print(m.group(1).strip('"\'')); break
else:
    print(f"{app}/data/flightboard.db")
PY
)"

# ---------------------------------------------------------------- 1. backup
say "Backup"
mkdir -p "$APP_DIR/backups"
if [ -f "$DB_PATH_VALUE" ]; then
  BACKUP="$APP_DIR/backups/flightboard-$(date -u +%Y%m%d-%H%M%S)-$$.db"
  "$PYTHON" - "$DB_PATH_VALUE" "$BACKUP" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
b = sqlite3.connect(dst)
a.backup(b)
ok = b.execute("PRAGMA integrity_check").fetchone()[0]
tables = {t: b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for (t,) in b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('trips','legs','airports')")}
b.close(); a.close()
os.chmod(dst, 0o600)
if ok != "ok":
    sys.exit(f"backup failed its integrity check: {ok}")
print(f"backup written and verified: {dst}")
print("rows in backup:", ", ".join(f"{k}={v}" for k, v in sorted(tables.items())) or "no known tables")
PY
else
  echo "no database at $DB_PATH_VALUE yet: nothing to back up (first run)"
fi

# ---------------------------------------------------------------- 3. dependencies
if [ "$SKIP_INSTALL" != 1 ]; then
  say "Dependencies"
  "$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "installed from requirements.txt"
fi

# ---------------------------------------------------------------- 4. airports
if [ "$SKIP_AIRPORTS" != 1 ]; then
  say "Airports"
  if [ -n "$AIRPORTS_FILE" ]; then
    DB_PATH="$DB_PATH_VALUE" "$PYTHON" -m scripts.update_airports --file "$AIRPORTS_FILE" --min-rows "$AIRPORTS_MIN_ROWS"
  else
    DB_PATH="$DB_PATH_VALUE" "$PYTHON" -m scripts.update_airports --min-rows "$AIRPORTS_MIN_ROWS"
  fi
fi

# ---------------------------------------------------------------- 5. one worker
say "Single worker check"
if [ -f "$UNIT_FILE" ]; then
  if grep -E '^\s*ExecStart=' "$UNIT_FILE" | grep -Eq -- '--workers[ =]*([2-9]|[1-9][0-9])'; then
    die "$UNIT_FILE starts more than one worker. Live status and rate limits live in memory: use exactly one."
  fi
  if grep -E '^\s*ExecStart=' "$UNIT_FILE" | grep -q -- '--proxy-headers'; then
    echo "warning: ExecStart passes --proxy-headers; the app reads Cloudflare's header itself, remove it unless you need it"
  fi
  echo "ok: $UNIT_FILE runs a single worker"
else
  echo "no unit file at $UNIT_FILE: skipped (set UNIT_FILE or run uvicorn with one worker yourself)"
fi

# ---------------------------------------------------------------- 6. restart
say "Restart"
if [ "$SKIP_RESTART" = 1 ]; then echo "restart skipped"; exit 0; fi
if [ "$RESTART_CMD" = "systemctl restart $SERVICE" ]; then systemctl daemon-reload; fi
eval "$RESTART_CMD"
for i in $(seq 1 30); do
  if curl -fs -o /dev/null --max-time 2 "$BASE/api/trips" 2>/dev/null; then echo "answering after ${i}s"; break; fi
  [ "$i" = 30 ] && die "service did not answer at $BASE within 30s. Check: journalctl -u $SERVICE -n 50 --no-pager"
  sleep 1
done

# ---------------------------------------------------------------- 7. smoke test
if [ "$SKIP_SMOKE" = 1 ]; then echo "smoke test skipped"; exit 0; fi
say "Smoke test"
fail=0
check() { if [ "$2" = ok ]; then echo "PASS  $1"; else echo "FAIL  $1: $3"; fail=1; fi; }

hdrs="$(curl -fsS -D - -o /dev/null "$BASE/")" || die "GET / failed"
echo "$hdrs" | grep -qi '^content-security-policy:.*script-src .self.' && check "page sends a Content-Security-Policy" ok || check "page sends a Content-Security-Policy" bad "header missing"

body="$(curl -fsS "$BASE/api/trips")" || die "GET /api/trips failed"
"$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); assert isinstance(d["trips"], list); print("PASS  board API answers with %d active trips" % len(d["trips"]))' "$body" || fail=1

if [ -n "$SMOKE_FLIGHT" ]; then
  day="$(date -u -d "+${SMOKE_DAYS} days" +%Y-%m-%d 2>/dev/null || date -u -v+"${SMOKE_DAYS}"d +%Y-%m-%d)"
  req="{\"rows\":[{\"kind\":\"flight\",\"flight_no\":\"$SMOKE_FLIGHT\",\"date\":\"$day\"}]}"
  resp="$(curl -sS -X POST -H 'content-type: application/json' --data "$req" "$BASE/api/legs/preview")" || die "preview request failed"
  "$PYTHON" - "$resp" "$SMOKE_FLIGHT" "$day" <<'PY' || fail=1
import json, sys
r = json.loads(sys.argv[1])["results"][0]
st = r["status"]
print(f"      {sys.argv[2]} on {sys.argv[3]}: status={st}  message={r['message']!r}")
if st == "ok":
    leg = r["leg"]; print(f"PASS  flight lookup works: {leg['from']} to {leg['to']}")
elif st == "not_found":
    print("WARN  the key works (the service answered) but this flight was not found that day; try SMOKE_FLIGHT with a real upcoming flight")
elif st in ("quota", "upstream_unavailable"):
    print("FAIL  the flight data service did not answer correctly (bad key, wrong AERODATABOX_AUTH, quota, or network)"); sys.exit(1)
else:
    print("FAIL  unexpected status"); sys.exit(1)
PY
else
  echo "skip  flight lookup (set SMOKE_FLIGHT=<a real upcoming flight number> to test the AeroDataBox key)"
fi

if command -v journalctl >/dev/null 2>&1 && [ "$RESTART_CMD" = "systemctl restart $SERVICE" ]; then
  echo; echo "Recent warnings in the service log:"
  journalctl -u "$SERVICE" -n 80 --no-pager 2>/dev/null | grep -iE 'warning|error' | tail -n 10 || echo "  none"
fi
echo
[ "$fail" = 0 ] && echo "Deploy checks passed." || { echo "Deploy checks FAILED."; exit 1; }
