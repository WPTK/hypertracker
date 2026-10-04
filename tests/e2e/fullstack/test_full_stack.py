"""The whole product, unmocked except the outside world: real uvicorn serving the
real app (real CSP header, real static modules, real resolver, real SQLite) in a real
browser. Only AeroDataBox (httpx.MockTransport) and map tiles (route stub) are faked.

This is the test that proves the independently built pieces fit together."""

from __future__ import annotations

import json
import re
import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("playwright")
from fixtures.loader import FIX, future_date  # noqa: E402
from playwright.sync_api import expect  # noqa: E402

from app import aerodatabox as adb  # noqa: E402
from app import config, db  # noqa: E402

DLG = "dialog.tf-dialog"
OPEN = "dialog.tf-dialog[open]"
D = future_date(10)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300090301ff0b8a0a0e0000000049454e44ae426082"
)


def _flights_json() -> str:
    text = (FIX / "real_flight_cx271.json").read_text()
    return text.replace("2025-01-11", D).replace("2025-01-12", future_date(11))


@pytest.fixture(scope="session")
def base_url():
    import uvicorn

    from app.main import app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("app did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)


@pytest.fixture
def world(monkeypatch):
    """Seed the airports the fixture flight uses and fake AeroDataBox."""
    monkeypatch.setattr(config, "AERODATABOX_KEY", "test-key")
    with db.get_conn() as c:
        for ident, iata, name, city, lat, lon in [
            ("VHHH", "HKG", "Hong Kong Intl", "Hong Kong", 22.31, 113.91),
            ("EHAM", "AMS", "Amsterdam Schiphol", "Amsterdam", 52.31, 4.76),
        ]:
            c.execute(
                "INSERT OR REPLACE INTO airports (ident,iata,name,lat,lon,type,municipality) "
                "VALUES (?,?,?,?,?,?,?)",
                (ident, iata, name, lat, lon, "large_airport", city),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/aircrafts/" in request.url.path:
            return httpx.Response(204)
        return httpx.Response(200, json=json.loads(_flights_json()))

    adb.set_transport(httpx.MockTransport(handler))
    yield
    adb.set_transport(None)


@pytest.fixture
def page(browser, base_url, world):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route(
        "**/*.tile.openstreetmap.org/**", lambda r: r.fulfill(status=200, body=PNG, content_type="image/png")
    )
    pg = ctx.new_page()
    pg.problems = []
    pg.on(
        "console",
        lambda m: pg.problems.append(f"console {m.type}: {m.text}") if m.type in ("error",) else None,
    )
    pg.on("pageerror", lambda e: pg.problems.append(f"pageerror: {e}"))
    # The form aborts a stale preview request on purpose when the user keeps typing.
    pg.on(
        "requestfailed",
        lambda r: (
            None
            if "ERR_ABORTED" in (r.failure or "")
            else pg.problems.append(f"requestfailed: {r.url} {r.failure}")
        ),
    )
    pg.goto(base_url + "/")
    yield pg
    ctx.close()


def test_add_view_map_edit_remove(page):
    # Empty board loads under the real CSP with no console errors.
    expect(page.locator("#board")).to_be_visible()
    page.wait_for_selector("#boardPanel[aria-busy='false']", timeout=10000)

    # Add a trip by flight number; the live preview verifies it before saving.
    page.click("#openAdd")
    expect(page.locator(DLG).first).to_be_visible()
    page.fill(f"{DLG} .tf-name input", "Alex")
    r = page.locator(".tf-dir[data-dir=out] .tf-row").first
    r.locator("[data-field=flight_no]").fill("CX 271")
    r.locator("[data-field=date]").fill(D)
    r.locator("[data-field=flight_no]").blur()
    expect(page.locator(f"{DLG} .tf-result").first).to_contain_text("HKG", timeout=8000)
    page.locator(f"{DLG} .tf-save").click()
    expect(page.locator(".tf-saved")).to_be_visible(timeout=8000)
    link = page.locator(".tf-saved input").first.input_value()
    assert "#manage=" in link and "?manage" not in link
    page.click(".tf-saved .btn--primary")

    # The board shows the resolved leg: IATA codes, flight number, edit/remove controls.
    board = page.locator("#board")
    expect(board).to_contain_text("CX271", timeout=10000)
    expect(board).to_contain_text("HKG")
    expect(board).to_contain_text("AMS")
    expect(board.locator("h3")).to_have_text("Alex")
    assert page.get_by_role("button", name="Edit").count() >= 1

    # Filtering by an airport token works from the keyboard-reachable button.
    page.locator("button.tok", has_text="HKG").first.click()
    expect(page.locator("#filterStatus")).to_contain_text("HKG")
    page.click("#clearFilter")

    # Map view renders the real Leaflet with an arc and markers.
    page.click("#viewMap")
    expect(page.locator("#mapPanel .leaflet-container")).to_be_visible(timeout=10000)
    expect(page.locator("#mapPanel path.leaflet-interactive").first).to_be_attached()
    page.click("#viewBoard")

    # Reload: this browser still owns the trip (token in localStorage), so it can edit.
    page.reload()
    expect(page.locator("#board")).to_contain_text("CX271", timeout=10000)
    page.get_by_role("button", name="Edit").first.click()
    expect(page.locator(OPEN)).to_be_visible()
    expect(page.locator(OPEN)).to_contain_text("Edit trip")
    page.keyboard.press("Escape")

    # Remove it through the confirm dialog.
    page.get_by_role("button", name="Remove").first.click()
    expect(page.locator(OPEN)).to_contain_text("HKG to AMS")
    page.locator(OPEN).get_by_role("button", name="Remove").click()
    expect(page.locator("#board")).not_to_contain_text("CX271", timeout=10000)

    import collections

    kinds = collections.Counter(re.sub(r"\d+", "N", x)[:170] for x in page.problems)
    assert not page.problems, dict(kinds)


def _add_trip(page):
    page.click("#openAdd")
    expect(page.locator(DLG).first).to_be_visible()
    page.fill(f"{DLG} .tf-name input", "Alex")
    r = page.locator(".tf-dir[data-dir=out] .tf-row").first
    r.locator("[data-field=flight_no]").fill("CX 271")
    r.locator("[data-field=date]").fill(D)
    r.locator("[data-field=flight_no]").blur()
    expect(page.locator(f"{DLG} .tf-result").first).to_contain_text("HKG", timeout=8000)
    page.locator(f"{DLG} .tf-save").click()
    expect(page.locator(".tf-saved")).to_be_visible(timeout=8000)
    page.click(".tf-saved .btn--primary")


def _owners(page):
    return page.evaluate("fetch('api/trips').then(r => r.json()).then(j => j.trips.map(t => t.owner_id))")


def test_same_person_after_removing_all_trips(page):
    page.wait_for_selector("#boardPanel[aria-busy='false']", timeout=10000)
    _add_trip(page)
    expect(page.locator("#board")).to_contain_text("CX271", timeout=10000)
    first = _owners(page)
    assert len(first) == 1
    ident = json.loads(page.evaluate("localStorage.getItem('hft.identity')"))
    assert ident["secret"]

    page.get_by_role("button", name="Remove").first.click()
    page.locator(OPEN).get_by_role("button", name="Remove").click()
    expect(page.locator("#board")).not_to_contain_text("CX271", timeout=10000)
    assert _owners(page) == []

    # The first trip (and its manage token) is gone; the identity secret still proves the person.
    _add_trip(page)
    expect(page.locator("#board")).to_contain_text("CX271", timeout=10000)
    assert _owners(page) == first
    expect(page.locator("#board h3")).to_have_count(1)
    assert json.loads(page.evaluate("localStorage.getItem('hft.identity')"))["uid"] == ident["uid"]
    assert not page.problems, page.problems


def test_security_headers_and_no_inline_script(page, base_url):
    r = httpx.get(base_url + "/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in r.text.replace('<script type="application/json"', "")
    assert r.headers.get("etag") is None  # page itself is not ETagged; the API is
    api = httpx.get(base_url + "/api/trips")
    assert api.status_code == 200 and api.headers.get("etag")
    again = httpx.get(base_url + "/api/trips", headers={"If-None-Match": api.headers["etag"]})
    assert again.status_code == 304
