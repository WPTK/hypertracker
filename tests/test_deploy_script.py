"""deploy/deploy.sh: backup, .env handling, single-worker guard. The service
restart, install, airports and smoke steps are skipped; they need a real host."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="deploy.sh is bash + Linux paths; on Windows deploy by hand (see deploy/README.md)",
)


@pytest.fixture
def install(tmp_path):
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    shutil.copy(ROOT / ".env.example", app / ".env.example")
    con = sqlite3.connect(app / "data" / "flightboard.db")
    con.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, owner_name TEXT)")
    con.execute("INSERT INTO trips (owner_name) VALUES ('Alex')")
    con.commit()
    con.close()
    (app / ".env").write_text(
        "SECRET_KEY=change-me-in-production\nAERODATABOX_KEY=keep-this-key\nTRUSTED_PROXY=none\nDEV_MODE=true\n"
    )
    return app


def run(app, **extra):
    env = {
        **os.environ,
        "APP_DIR": str(app),
        "PYTHON": sys.executable,
        "SKIP_INSTALL": "1",
        "SKIP_AIRPORTS": "1",
        "SKIP_SMOKE": "1",
        "SKIP_RESTART": "1",
        "UNIT_FILE": str(app / "no-unit"),
        **extra,
    }
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy.sh")], env=env, capture_output=True, text=True, check=False
    )


def env_value(app, key):
    for line in (app / ".env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def test_first_run_sets_env_and_backs_up(install):
    r = run(install)
    assert r.returncode == 0, r.stderr + r.stdout
    secret = env_value(install, "SECRET_KEY")
    assert secret and secret != "change-me-in-production" and len(secret) >= 48
    assert secret not in r.stdout + r.stderr  # secrets are never printed
    assert env_value(install, "TRUSTED_PROXY") == "cloudflare"
    assert env_value(install, "DEV_MODE") == "false"
    assert env_value(install, "AERODATABOX_KEY") == "keep-this-key"
    assert oct((install / ".env").stat().st_mode & 0o777) == "0o600"
    (backup,) = (install / "backups").glob("flightboard-*.db")
    assert sqlite3.connect(backup).execute("SELECT owner_name FROM trips").fetchone() == ("Alex",)


def test_second_run_keeps_the_secret_and_adds_a_backup(install):
    run(install)
    first = env_value(install, "SECRET_KEY")
    assert run(install).returncode == 0
    assert env_value(install, "SECRET_KEY") == first
    assert len(list((install / "backups").glob("flightboard-*.db"))) == 2


def test_refuses_more_than_one_worker(install, tmp_path):
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nExecStart=/x/uvicorn app.main:app --workers 4\n")
    r = run(install, UNIT_FILE=str(unit))
    assert r.returncode != 0 and "more than one worker" in r.stderr


def test_first_install_without_a_database_still_works(install):
    (install / "data" / "flightboard.db").unlink()
    r = run(install)
    assert r.returncode == 0 and "nothing to back up" in r.stdout
