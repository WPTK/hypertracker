"""Playwright tests for app/static/js/map.js.

Self-contained: serves tests/e2e/map/harness plus app/static from a tiny local
HTTP server, stubs OSM tiles with a generated PNG, and skips cleanly when
Playwright or Chromium is unavailable. Never downloads browsers.
"""

import glob
import http.server
import re
import struct
import threading
import zlib
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

HERE = Path(__file__).resolve().parent
STATIC = HERE.parents[2] / "app" / "static"
HARNESS = HERE / "harness"
SHOTS = HERE / "screenshots"


def _png(w=256, h=256):
    """Pale OSM-like tile with a darker outline so tile seams are visible."""
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            edge = x in (0, w - 1) or y in (0, h - 1)
            row += bytes((190, 200, 205) if edge else (242, 239, 233))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


TILE = _png()


class _Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html"):
            return str(HARNESS / "index.html")
        if path.startswith("/static/"):
            return str(STATIC / path[len("/static/") :])
        if path.startswith("/harness/"):
            return str(HARNESS / path[len("/harness/") :])
        return str(HARNESS / "__missing__")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _chromium_path():
    cands = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")) + sorted(
        glob.glob("/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell")
    )
    return cands[0] if cands else None


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        exe = _chromium_path()
        try:
            b = (
                p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                if exe
                else p.chromium.launch(args=["--no-sandbox"])
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


def _open(browser, base_url, *, size=(1100, 800), theme="dark", tiles="ok", block_leaflet=False):
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def tile(route):
        if tiles == "ok":
            route.fulfill(status=200, content_type="image/png", body=TILE)
        else:
            route.fulfill(status=404, body="")

    page.route(re.compile(r"https://[a-z]*\.?tile\.openstreetmap\.org/.*"), tile)
    if block_leaflet:
        page.route("**/vendor/leaflet/leaflet.js", lambda r: r.abort())
    page.goto(f"{base_url}/?theme={theme}")
    page.wait_for_function("window.H && window.H.ready")
    page.errors = errors
    return ctx, page


def _show(page, data="SAMPLE", flt=None):
    page.evaluate(
        "([d, f]) => { H.m.update(H[d], f); H.m.show(); }", [data, flt or {"tok": None, "date": None}]
    )
    page.wait_for_function("H.state().ready")
    page.wait_for_selector(".hmap-pin")
    page.wait_for_timeout(150)


def _inside(page, codes):
    return page.evaluate(
        """(codes) => { const s = H.m._debug(); const b = s.map.getBounds();
          const all = [].concat(H.SAMPLE, H.DATELINE, H.OTHER_SET).flatMap(t => t.out.concat(t.ret));
          return codes.map(c => { const l = all.find(x => x.from === c || x.to === c);
            const lat = l.from === c ? l.from_lat : l.to_lat, lon = l.from === c ? l.from_lon : l.to_lon;
            return [c, b.contains([lat, lon]) || b.contains([lat, lon + 360]) || b.contains([lat, lon - 360])]; }); }""",
        codes,
    )


def _style(page):
    out = {}
    for a in page.evaluate("H.state().arcs"):
        color, weight, dash, opacity = a["styleKey"].split("|")
        out[a["key"]] = {"color": color, "weight": float(weight), "dash": dash, "opacity": float(opacity)}
    return out


def _drag(page):
    box = page.locator(".hmap__map").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 60
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx - 90, cy + 20, steps=6)
    page.mouse.up()
    page.wait_for_timeout(400)


def test_first_draw_fits_all_airports(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    assert all(ok for _, ok in _inside(page, ["KJAX", "KATL", "KDEN", "KLAX", "YSSY"]))
    assert set(page.evaluate("H.state().pins")) == {"KJAX", "KATL", "KDEN", "KLAX", "YSSY"}
    assert page.errors == []
    ctx.close()


def test_update_same_data_keeps_user_view(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    before = page.evaluate("H.state().center")
    # Same data again without user input must not move anything either.
    page.evaluate("H.m.update(JSON.parse(JSON.stringify(H.SAMPLE)), {tok:null,date:null})")
    assert page.evaluate("H.state().center") == before
    _drag(page)
    moved = page.evaluate("H.state()")
    assert moved["userMoved"] and moved["center"] != before
    page.evaluate("H.m.update(JSON.parse(JSON.stringify(H.SAMPLE)), {tok:'DL1200',date:null})")
    assert page.evaluate("H.state().center") == moved["center"]
    assert page.evaluate("H.state().zoom") == moved["zoom"]
    # Even a different airport set does not refit after a user move...
    page.evaluate("H.m.update(H.OTHER_SET, {tok:null,date:null})")
    assert page.evaluate("H.state().center") == moved["center"]
    # ...until the user presses Fit all.
    page.get_by_role("button", name="Fit all").click()
    assert all(ok for _, ok in _inside(page, ["EGLL", "KATL"]))
    ctx.close()


def test_new_airport_set_refits(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    c0 = page.evaluate("H.state().center")
    page.evaluate("H.m.update(H.OTHER_SET, {tok:null,date:null})")
    assert page.evaluate("H.state().center") != c0
    assert all(ok for _, ok in _inside(page, ["EGLL", "KATL"]))
    assert set(page.evaluate("H.state().pins")) == {"EGLL", "KATL"}
    ctx.close()


def test_filter_highlight_styles(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    base = _style(page)
    assert base["out:1:0"]["weight"] > base["out:1:1"]["weight"]  # airborne thicker than scheduled
    assert base["ret:1:0"]["dash"] not in ("", "null") and base["out:1:0"]["dash"] in (
        "",
        "null",
    )  # on ground dashed
    page.evaluate("H.m.update(H.SAMPLE, {tok:'KDEN',date:null})")
    s = _style(page)
    hits = {k for k, v in s.items() if v["opacity"] == 1}
    assert hits == {"out:1:1", "ret:1:0"}  # legs touching KDEN
    dim = [v for k, v in s.items() if k not in hits]
    assert dim and all(0 < v["opacity"] < 0.5 for v in dim)  # de-emphasised but still visible
    assert s["out:1:1"]["weight"] > base["out:1:1"]["weight"]
    assert s["out:1:1"]["color"] != base["out:1:1"]["color"]
    lit = page.evaluate(
        "[...document.querySelectorAll('.hmap-pin.is-lit')].filter(n => n.getAttribute('aria-hidden') !== 'true').map(n => n.getAttribute('aria-label').slice(0,4)).sort()"
    )
    assert lit == ["KATL", "KDEN", "KJAX"]
    page.evaluate("H.m.update(H.SAMPLE, {tok:null,date:'2026-10-11'})")
    s = _style(page)
    assert {k for k, v in s.items() if v["opacity"] == 1} == {"out:2:0"}
    ctx.close()


def test_date_chips_and_callback(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    chips = page.locator(".hmap__chips .chip")
    assert chips.all_inner_texts() == ["2026-10-10", "2026-10-11", "2026-10-14"]
    chips.nth(1).click()
    assert page.evaluate("H.calls.dates") == ["2026-10-11"]
    assert "is-active" in chips.nth(1).get_attribute("class")
    assert chips.nth(1).get_attribute("aria-pressed") == "true"
    assert {k for k, v in _style(page).items() if v["opacity"] == 1} == {"out:2:0"}
    chips.nth(1).click()
    assert page.evaluate("H.calls.dates") == ["2026-10-11", None]
    ctx.close()


def test_marker_click_and_keyboard(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    pin = page.locator('.hmap-pin[aria-label^="KDEN"]')
    assert pin.get_attribute("role") == "button"
    assert "Denver" in pin.get_attribute("aria-label")
    pin.click()
    assert page.evaluate("H.calls.codes") == ["KDEN"]
    pin.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("H.calls.codes") == ["KDEN", "KDEN"]
    # accessible text alternative
    items = page.locator(".hmap-sr li").all_inner_texts()
    assert any(t.startswith("DL1200 KJAX to KATL") for t in items)
    assert page.locator(".hmap__map").get_attribute("aria-label")
    ctx.close()


def test_theme_filter_only_on_tiles(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    f = "(sel) => getComputedStyle(document.querySelector(sel)).filter"
    assert "invert" in page.evaluate(f, ".leaflet-tile-pane")
    assert page.evaluate(f, ".leaflet-overlay-pane") == "none"
    assert page.evaluate(f, ".leaflet-marker-pane") == "none"
    dark = _style(page)["out:1:1"]["color"]
    page.evaluate("H.setPage('light')")
    page.wait_for_timeout(100)
    assert page.evaluate(f, ".leaflet-tile-pane") == "none"
    assert _style(page)["out:1:1"]["color"] != dark  # colours re-read per theme
    page.evaluate("H.setPage('dark')")
    assert "invert" in page.evaluate(f, ".leaflet-tile-pane")
    ctx.close()


def test_leaflet_load_failure_shows_message_and_retry(browser, base_url):
    ctx, page = _open(browser, base_url, block_leaflet=True)
    page.evaluate("H.m.update(H.SAMPLE, {tok:null,date:null}); H.m.show()")
    msg = page.locator(".hmap__msg")
    msg.wait_for(state="visible")
    assert "The map couldn't load. The board still works." in msg.inner_text()
    assert msg.get_attribute("role") == "status"
    assert page.errors == []
    # chips and the text alternative still work without Leaflet
    assert page.locator(".hmap__chips .chip").count() == 3
    page.unroute("**/vendor/leaflet/leaflet.js")
    page.get_by_role("button", name="Retry").click()
    page.wait_for_function("H.state().ready")
    page.wait_for_selector(".hmap-pin")
    assert not msg.is_visible()
    ctx.close()


def test_dateline_arcs_have_no_world_spanning_segment(browser, base_url):
    ctx, page = _open(browser, base_url)
    for a, b in [((33.94, -118.41), (-33.95, 151.18)), ((21.32, -157.92), (35.76, 140.39))]:
        path = page.evaluate("([a, b]) => H.gcPath(a[0], a[1], b[0], b[1])", [a, b])
        steps = [abs(path[i + 1][1] - path[i][1]) for i in range(len(path) - 1)]
        assert max(steps) < 20, steps
        assert all(isinstance(p[0], (int, float)) for p in path)
    # degenerate inputs never produce NaN/Infinity or throw
    assert page.evaluate("H.gcPath(10, 10, 10, 10)") == []
    anti = page.evaluate("H.gcPath(0, 0, 0, 180)")
    assert anti and all(x == x and abs(x) < 1e6 for p in anti for x in p)

    _show(page, "DATELINE")
    d = page.evaluate(
        "[...document.querySelectorAll('.leaflet-overlay-pane path')].map(p => p.getAttribute('d'))"
    )
    zoom = page.evaluate("H.state().zoom")
    half_world_px = 256 * (2**zoom) / 2
    assert len(d) == 2
    for path_d in d:
        subs = [s for s in path_d.split("M") if s.strip()]
        assert 1 <= len(subs) <= 3  # world copies (offscreen ones are clipped)
        for sub in subs:
            xs = [float(m) for m in re.findall(r"[ML]?\s*(-?[\d.]+)[ ,]", sub + " ")][::2]
            assert max(abs(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)) < half_world_px
    assert all(ok for _, ok in _inside(page, ["KLAX", "YSSY", "PHNL", "RJAA"]))
    ctx.close()


def test_tile_failures_show_notice(browser, base_url):
    ctx, page = _open(browser, base_url, tiles="fail")
    _show(page)
    page.locator(".hmap__notice").wait_for(state="visible", timeout=10000)
    assert page.locator(".hmap__notice").get_attribute("role") == "status"
    assert page.locator(".hmap-pin").count() > 0  # still usable
    ctx.close()


def test_hide_show_and_destroy(browser, base_url):
    ctx, page = _open(browser, base_url)
    _show(page)
    c0 = page.evaluate("H.state().center")
    page.evaluate("H.m.hide()")
    assert not page.locator(".hmap").is_visible()
    page.evaluate("H.m.update(H.SAMPLE, {tok:'DL1200',date:null}); H.m.show()")
    page.wait_for_timeout(200)
    assert page.evaluate("H.state().center") == c0
    page.evaluate("H.m.destroy(); H.m.update(H.SAMPLE, {}); H.m.show()")
    assert page.errors == []
    ctx.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("name,size", [("desktop", (1280, 860)), ("mobile", (390, 844))])
def test_screenshots(browser, base_url, theme, name, size):
    ctx, page = _open(browser, base_url, size=size, theme=theme)
    _show(page)
    all_trips = "H.SAMPLE.concat(H.DATELINE.map(t => ({...t, id: t.id + 10})))"
    page.evaluate(f"H.m.update({all_trips}, {{tok:null,date:null}})")
    page.wait_for_timeout(300)
    SHOTS.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOTS / f"map-{theme}-{name}-all.png"), full_page=True)
    page.evaluate(f"H.m.update({all_trips}, {{tok:'DL1200',date:null}})")
    page.wait_for_timeout(300)
    if name == "mobile":
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(SHOTS / f"map-{theme}-{name}-selected.png"), full_page=True)
    ctx.close()
