"""Fixtures for the shell e2e tests: a live harness server and a Chromium page.

Everything skips cleanly when Playwright or Chromium is not available."""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SHOTS = HERE / "screenshots"


def _chromium_path() -> str | None:
    env = os.environ.get("CHROMIUM_PATH")
    if env and os.path.exists(env):
        return env
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell",
                    "chromium-*/chrome-linux64/chrome"):
        hits = sorted(glob.glob(os.path.join(base, pattern)))
        if hits:
            return hits[-1]
    return shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


@pytest.fixture(scope="session")
def base_url():
    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    import uvicorn
    import harness_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(harness_app.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.skip("harness server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    exe = _chromium_path()
    if not exe:
        pytest.skip("Chromium not found (set CHROMIUM_PATH or PLAYWRIGHT_BROWSERS_PATH)")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Chromium would not launch: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture()
def harness(base_url):
    """Reset harness state and give tests a way to change it."""
    import httpx

    def set_state(**kw):
        httpx.post(base_url + "/__test/set", json=kw).raise_for_status()

    set_state(mode="full", fail=False, log=[], user=None, admin=False, t0=time.time())
    set_state.url = base_url
    set_state.log = lambda: httpx.get(base_url + "/__test/log").json()
    return set_state


@pytest.fixture()
def make_page(browser, base_url, harness):
    """Factory: make_page(width=1280, height=900, theme='dark', clock=False) -> (page, errors)."""
    contexts = []

    def factory(width=1280, height=900, theme="dark", clock=False, scheme=None, reduced=False, mobile=False, bypass_csp=False):
        ctx = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=scheme or "no-preference",
            reduced_motion="reduce" if reduced else "no-preference",
            has_touch=mobile, is_mobile=mobile, bypass_csp=bypass_csp,
        )
        contexts.append(ctx)
        if theme in ("dark", "light"):
            ctx.add_init_script(f"try{{ if(!localStorage.getItem('wptk-theme')) localStorage.setItem('wptk-theme','{theme}') }}catch(e){{}}")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url}"))
        if clock:
            page.clock.install()
        return page, errors

    yield factory
    for c in contexts:
        c.close()


@pytest.fixture(scope="session")
def axe_js():
    """axe-core source from AXE_PATH, a local cache, or `npm pack axe-core`. Skips the axe tests if unavailable."""
    env = os.environ.get("AXE_PATH")
    if env and os.path.exists(env):
        return Path(env).read_text()
    cache = Path(tempfile.gettempdir()) / "hypertracker-axe" / "package" / "axe.min.js"
    if cache.exists():
        return cache.read_text()
    if not shutil.which("npm"):
        pytest.skip("axe-core unavailable: no npm and no AXE_PATH (manual a11y checks still run)")
    d = cache.parent.parent
    d.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["npm", "pack", "axe-core", "--silent"], cwd=d, check=True, timeout=120,
                       capture_output=True)
        tgz = sorted(d.glob("axe-core-*.tgz"))[-1]
        subprocess.run(["tar", "xzf", str(tgz)], cwd=d, check=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"axe-core unavailable ({e}); manual a11y checks still run")
    return cache.read_text()
