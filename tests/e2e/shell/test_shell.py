"""Playwright tests for the board shell (template, CSS, main.js, api.js).

Run: pytest tests/e2e/shell -q
Screenshots land in tests/e2e/shell/screenshots/."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SHOTS = HERE / "screenshots"
SHOTS.mkdir(exist_ok=True)


def load(page, url, people=5):
    page.goto(url)
    page.wait_for_selector(".person")
    assert page.locator(".person").count() == people


# ---------- basics ----------


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(390, 844), (1280, 900)], ids=["390", "1280"])
def test_renders_clean_and_screenshots(make_page, base_url, theme, size):
    page, errors = make_page(width=size[0], height=size[1], theme=theme, mobile=size[0] < 500)
    load(page, base_url + "/")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == (
        "light" if theme == "light" else None
    )
    assert page.locator("h1").count() == 1
    assert page.locator("main").count() == 1
    # no horizontal scroll
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # every dep/day label stays on one line
    assert page.evaluate("""() => [...document.querySelectorAll('.leg__day,.leg__dep')]
        .every(e => e.getBoundingClientRect().height < 2.2 * parseFloat(getComputedStyle(e).lineHeight || 24))""")
    page.screenshot(path=str(SHOTS / f"board-{size[0]}-{theme}.png"), full_page=True)
    assert errors == []


def test_content_of_legs(make_page, base_url):
    page, errors = make_page()
    load(page, base_url + "/")
    # In the air strip
    strip = page.locator("#airborne")
    assert strip.is_visible()
    assert "Alex" in strip.inner_text() and "DL1200" in strip.inner_text()
    # status chips exist with text labels
    labels = set(page.locator(".leg .status").all_inner_texts())
    assert {"Airborne", "On ground", "Scheduled", "Landed", "Unverified"} <= labels
    # IATA primary, city secondary, arrival, relative text
    first = page.locator(".leg").first
    txt = first.inner_text()
    assert "DEN" in txt and "JAX" in txt and "Denver to Jacksonville" in txt
    assert "Arrives" in txt and "lands in" in txt
    assert "Boeing 737-900" in txt and "6 years old" in txt and "N841DN" in txt
    # FlightAware: only where fa_url exists, accessible name, safe attrs
    fa = page.get_by_role("link", name="FlightAware for DL1200").first
    assert fa.get_attribute("target") == "_blank" and "noopener" in fa.get_attribute("rel")
    dana = page.locator(".person", has_text="Dana")
    assert dana.get_by_role("link", name="FlightAware").count() == 0
    assert "Entered by hand" in dana.inner_text()
    assert (
        "Today" in page.locator(".leg__day").first.inner_text()
        or "Yesterday" in page.locator(".leg__day").first.inner_text()
    )
    assert errors == []


def test_edit_remove_only_for_owner(make_page, base_url):
    page, _ = make_page()
    load(page, base_url + "/")
    # "me" is Alex, who has two trips
    assert page.get_by_role("button", name="Edit Alex's trip", exact=False).count() == 2
    assert page.get_by_role("button", name="Remove Alex's trip", exact=False).count() == 2
    assert page.get_by_role("button", name="Edit Bailey's trip", exact=False).count() == 0
    page.get_by_role("button", name="Edit Alex's trip, DEN to JAX").click()
    assert page.evaluate("window.__formCalls") == [["edit", 1]]
    page.get_by_role("button", name="Close stub").click()
    page.get_by_role("button", name="Remove Alex's trip, DEN to JAX").click()
    assert page.evaluate("window.__formCalls")[-1] == ["remove", 1]


def test_touch_targets_44px(make_page, base_url):
    page, _ = make_page(width=390, height=844, mobile=True)
    load(page, base_url + "/")
    small = page.evaluate("""() => [...document.querySelectorAll('.btn, .chip, .wptk-theme-toggle')]
        .filter(e => e.offsetParent).map(e => [e.className, Math.round(e.getBoundingClientRect().height)])
        .filter(x => x[1] < 44)""")
    assert small == []


# ---------- keyboard, filter, focus ----------


def test_keyboard_filter(make_page, base_url):
    page, errors = make_page()
    load(page, base_url + "/")
    status = page.locator("#filterStatus")
    assert status.inner_text().strip() == ""
    # reach a token by keyboard only
    for _ in range(60):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.classList.contains('tok')"):
            break
    else:
        pytest.fail("never reached a token button by keyboard")
    code = page.evaluate("document.activeElement.dataset.tok")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.getAttribute('aria-pressed')") == "true"
    assert code in status.inner_text() and "match" in status.inner_text()
    assert page.locator(".leg.is-hit").count() >= 1
    assert page.locator(".leg.is-dim").count() >= 1
    assert page.locator("#clearFilter").is_visible()
    # toggle off with the same key
    page.keyboard.press("Enter")
    assert page.locator(".leg.is-hit").count() == 0
    assert page.locator("#clearFilter").is_hidden()
    # Clear button path
    page.keyboard.press("Enter")
    page.locator("#clearFilter").focus()
    page.keyboard.press("Enter")
    assert page.locator(".leg.is-dim").count() == 0
    assert errors == []


def test_day_chip_filter(make_page, base_url):
    page, _ = make_page()
    load(page, base_url + "/")
    chips = page.locator("#dayChips button.chip")
    assert chips.count() >= 2
    chips.nth(0).focus()
    page.keyboard.press("Enter")
    assert page.locator("#dayChips button.chip").nth(0).get_attribute("aria-pressed") == "true"
    assert page.locator(".leg.is-hit").count() >= 1


def test_focus_preserved_and_filter_auto_clears(make_page, base_url, harness):
    page, errors = make_page(clock=True)
    load(page, base_url + "/")
    page.locator('button.tok[data-tok="DEN"]').first.focus()
    key = page.evaluate("document.activeElement.dataset.key")
    harness(mode="extra")
    page.clock.run_for(61000)
    page.wait_for_selector(".person:has-text('Fay')")
    assert page.evaluate("document.activeElement.dataset.key") == key
    # now filter on a code that disappears
    page.locator('button.tok[data-tok="DEN"]').first.click()
    assert page.locator(".leg.is-hit").count() >= 1
    page.locator(".tok", has_text="ATL").first.click()  # ATL only appears in the extra and connection legs
    harness(mode="empty")
    page.clock.run_for(61000)
    page.wait_for_selector(".empty")
    assert page.locator(".leg.is-dim").count() == 0
    assert (
        "cleared the filter" in page.locator("#filterStatus").inner_text()
        or "Nothing on the board" in page.locator("#filterStatus").inner_text()
    )
    assert page.get_by_role("button", name="Add a trip").count() == 2
    assert errors == []


# ---------- polling ----------


def test_304_keeps_dom_and_sends_etag(make_page, base_url, harness):
    page, _ = make_page(clock=True)
    load(page, base_url + "/")
    page.evaluate("document.querySelector('.person').__mark = 1")
    page.clock.run_for(61000)
    page.wait_for_timeout(300)
    log = harness.log()
    assert len(log) >= 2 and log[0]["inm"] is None and log[1]["inm"]
    assert page.evaluate("document.querySelector('.person').__mark") == 1


def test_failed_poll_keeps_board_and_shows_banner(make_page, base_url, harness):
    page, errors = make_page(clock=True)
    load(page, base_url + "/")
    harness(fail=True)
    page.clock.run_for(61000)
    page.wait_for_selector("#staleBanner:not([hidden])")
    assert page.locator(".person").count() == 5
    assert "Could not refresh the board" in page.locator("#staleText").inner_text()
    harness(fail=False)
    page.get_by_role("button", name="Try again now").click()
    page.wait_for_selector("#staleBanner", state="hidden")
    assert page.locator(".person").count() == 5
    # the 503 itself is the only console noise allowed
    assert all("503" in e for e in errors), errors


def test_first_load_failure_has_retry(make_page, base_url, harness):
    harness(fail=True)
    page, errors = make_page(clock=True)
    page.goto(base_url + "/")
    page.wait_for_selector("text=Could not load the board")
    assert page.locator(".person").count() == 0
    harness(fail=False)
    page.get_by_role("button", name="Try again").click()
    page.wait_for_selector(".person")
    assert page.locator(".person").count() == 5


def test_polling_pauses_while_dialog_open(make_page, base_url, harness):
    page, _ = make_page(clock=True)
    load(page, base_url + "/")
    n0 = len(harness.log())
    page.get_by_role("button", name="Add a trip").first.click()
    assert page.locator("dialog[open]").count() == 1
    page.clock.run_for(130000)
    page.wait_for_timeout(300)
    assert len(harness.log()) == n0
    page.get_by_role("button", name="Close stub").click()
    page.clock.run_for(4000)  # the held-back poll checks again every 3s
    page.wait_for_timeout(500)
    assert len(harness.log()) > n0


def test_hidden_tab_pauses_polling(make_page, base_url, harness):
    page, _ = make_page(clock=True)
    load(page, base_url + "/")
    n0 = len(harness.log())
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
      document.dispatchEvent(new Event('visibilitychange')); }""")
    page.clock.run_for(180000)
    page.wait_for_timeout(300)
    assert len(harness.log()) == n0


def test_relative_text_updates_without_rerender(make_page, base_url):
    page, _ = make_page(clock=True)
    load(page, base_url + "/")
    page.evaluate("document.querySelector('.leg').__mark = 7")
    before = page.locator(".leg").first.locator(".leg__rel").inner_text()
    page.clock.fast_forward(30 * 60 * 1000)
    page.clock.run_for(31000)
    after = page.locator(".leg").first.locator(".leg__rel").inner_text()
    assert before != after
    assert page.evaluate("document.querySelector('.leg').__mark") == 7


# ---------- view, theme ----------


def test_map_view_and_theme_wiring(make_page, base_url, wait_until):
    page, errors = make_page()
    load(page, base_url + "/")
    assert page.locator("#mapPanel").is_hidden()
    page.get_by_role("button", name="Map").click()
    page.wait_for_selector("#mapPanel:not([hidden])")
    assert page.locator("#boardPanel").is_hidden()
    assert page.get_by_role("button", name="Map").get_attribute("aria-pressed") == "true"
    # The map module is loaded with a dynamic import(); on a slow runner it can still be in
    # flight when the panel appears, so wait for the calls instead of reading them at once.
    wait_until(
        page,
        "!!window.__mapCalls && ['setTheme','show','update'].every(n => window.__mapCalls.some(c => c[0] === n))",
    )
    calls = [c[0] for c in page.evaluate("window.__mapCalls")]
    assert "setTheme" in calls and "show" in calls and "update" in calls
    page.get_by_role("button", name=" theme. Switch to light.", exact=False).click()
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert ["setTheme", "light"] in page.evaluate("window.__mapCalls")
    # a map-originated selection flows into the board filter and back to the map
    page.evaluate("window.__mapOpts.onSelectCode('KDEN')")
    page.get_by_role("button", name="Board").click()
    assert page.locator('button.tok[data-tok="DEN"]').first.get_attribute("aria-pressed") == "true"
    page.get_by_role("button", name="Map").click()
    last = [c for c in page.evaluate("window.__mapCalls") if c[0] == "update"][-1][1]
    assert last["filter"]["tok"] == "KDEN"
    assert errors == []


def test_theme_persists_and_first_visit_honours_system(make_page, base_url):
    page, _ = make_page(theme=None, scheme="light")
    load(page, base_url + "/")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    page.get_by_role("button", name=" theme. Switch to dark.", exact=False).click()
    assert page.evaluate("localStorage.getItem('wptk-theme')") == "dark"
    page.reload()
    page.wait_for_selector(".person")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") is None


def test_gated_and_auth_messages(make_page, base_url, harness):
    page, errors = make_page()
    page.goto(base_url + "/gated?auth=unavailable")
    assert page.get_by_text("This board is members-only").is_visible()
    assert "isn't set up" in page.locator("#authNote").inner_text()
    assert page.locator("#board").count() == 0
    page.goto(base_url + "/?auth=not_member")
    page.wait_for_selector(".person")
    assert "isn't in the server" in page.locator("#authNote").inner_text()
    assert errors == []


def test_manage_fragment_seeded(make_page, base_url):
    page, _ = make_page()
    page.goto(base_url + "/#manage=12.abc")
    page.wait_for_selector(".person")
    assert page.evaluate("location.hash") == ""
    assert "Manage link saved" in page.locator("#authNote").inner_text()


# ---------- accessibility ----------


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_axe(make_page, base_url, axe_js, theme):
    page, _ = make_page(theme=theme, bypass_csp=True)  # axe is injected inline
    load(page, base_url + "/")
    page.add_script_tag(content=axe_js)
    res = page.evaluate("""async () => (await axe.run(document, {runOnly: {type: 'tag',
        values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa','best-practice']}})).violations
        .map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target.join(' ')).slice(0, 4)}))""")
    assert res == [], res


def test_reduced_motion_has_no_transitions(make_page, base_url):
    page, _ = make_page(reduced=True)
    load(page, base_url + "/")
    durations = page.evaluate("""() => [...document.querySelectorAll('.btn,.chip,.hub-card,.wptk-theme-toggle,.wptk-theme-toggle__thumb')]
        .map(e => getComputedStyle(e).transitionDuration).filter(d => d !== '0s')""")
    assert durations == []


def test_contrast_script_passes():
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "contrast_check.py")], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout + r.stderr
