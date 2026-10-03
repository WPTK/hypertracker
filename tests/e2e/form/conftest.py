"""Fixtures for the trip form browser tests. Skips cleanly without Chromium."""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - playwright not installed
    sync_playwright = None

SHOTS = Path(__file__).resolve().parent / "screenshots"


@pytest.fixture(scope="session")
def server():
    from mock_server import Server
    srv = Server()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed")
    pw = sync_playwright().start()
    br = None
    try:
        try:
            br = pw.chromium.launch()
        except Exception:
            candidates = sorted(glob.glob("/opt/pw-browsers/chromium*/chrome-linux/chrome")
                                + glob.glob("/opt/pw-browsers/chromium*/chrome-linux/headless_shell"))
            for exe in candidates:
                try:
                    br = pw.chromium.launch(executable_path=exe)
                    break
                except Exception:
                    continue
        if br is None:
            pytest.skip("Chromium is not available")
        yield br
    finally:
        if br is not None:
            br.close()
        pw.stop()


@pytest.fixture()
def api(server):
    """httpx client against the mock's control endpoints; resets state first."""
    c = httpx.Client(base_url=server.url, timeout=10)
    c.post("/__reset")
    yield c
    c.close()


@pytest.fixture()
def make_page(browser, server, api):
    contexts = []

    def _make(width=1000, height=800, user=False, context=None):
        ctx = context or browser.new_context(
            viewport={"width": width, "height": height}, timezone_id="UTC", locale="en-US",
            permissions=["clipboard-read", "clipboard-write"], accept_downloads=False)
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.base = server.url
        page.goto(server.url + ("/?user=1" if user else "/"))
        page.wait_for_selector("html[data-ready='1']")
        return page

    yield _make
    for c in contexts:
        c.close()


@pytest.fixture()
def page(make_page):
    p = make_page()
    yield p
    assert not p.errors, p.errors


@pytest.fixture()
def shot():
    SHOTS.mkdir(exist_ok=True)

    def _shot(page, name):
        page.screenshot(path=str(SHOTS / f"{name}.png"))

    return _shot
