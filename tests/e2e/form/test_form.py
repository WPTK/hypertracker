"""Browser tests for the trip form (app/static/js/tripForm.js, identity.js).

Run: python -m pytest tests/e2e/form -q
They use a mock API (mock_server.py) and a harness page; they skip when
Playwright or Chromium is unavailable.
"""
from __future__ import annotations

import json
import re
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

TODAY = time.strftime("%Y-%m-%d", time.gmtime())
TOK_A = "tokenAAAAAAAAAAAA01"
TOK_B = "tokenBBBBBBBBBBBB02"
TOK_C = "tokenCCCCCCCCCCCC03"

DLG = "dialog.tf-dialog"


# ---------------------------------------------------------------- helpers
def dialog(page):
    return page.locator(DLG).first


def open_new(page):
    page.click("#openAdd")
    expect(dialog(page)).to_be_visible()


def row(page, n=0, d="out"):
    return page.locator(f".tf-dir[data-dir={d}] .tf-row").nth(n)


def f(r, name):
    return r.locator(f"[data-field={name}]")


def set_name(page, name="Sam"):
    page.fill(f"{DLG} .tf-name input", name)


def type_flight(r, flight, **kw):
    f(r, "flight_no").fill(flight)
    for k, v in kw.items():
        f(r, k).fill(v)


def save(page):
    dialog(page).locator(".tf-save").click()


def stats(api):
    return api.get("/__stats").json()


def seed_and_remember(page, api, out, ret=None, name="Sam"):
    """Create a trip server-side and hand this browser its manage token."""
    r = api.post("/__seed", json={"out": out, "ret": ret or [], "name": name}).json()
    page.evaluate(
        """async ([id, tok]) => { const m = await import('/static/js/identity.js'); m.rememberManage(id, tok); }""",
        [r["trip_id"], r["token"]],
    )
    page.reload()
    page.wait_for_selector("html[data-ready='1']")
    return r


# ---------------------------------------------------------------- dialog behaviour
def test_escape_closes_and_focus_returns_to_opener(page):
    page.focus("#openAdd")
    page.keyboard.press("Enter")
    expect(dialog(page)).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog(page)).not_to_be_visible()
    assert page.evaluate("document.activeElement.id") == "openAdd"
    assert not page.evaluate("document.documentElement.classList.contains('has-dialog')")


def test_dialog_is_labelled_and_modal(page):
    open_new(page)
    d = dialog(page)
    labelled = d.get_attribute("aria-labelledby")
    assert page.locator(f"#{labelled}").inner_text() == "Add a trip"
    described = d.get_attribute("aria-describedby")
    assert page.locator(f"#{described}").inner_text().strip() != ""
    assert page.evaluate("document.querySelector('dialog.tf-dialog').matches(':modal')")
    assert page.evaluate("getComputedStyle(document.documentElement).overflow") == "hidden"


def test_first_field_focused_and_trap(page):
    open_new(page)
    assert page.evaluate("document.activeElement.closest('.tf-name') !== null")
    for key in ["Tab"] * 30 + ["Shift+Tab"] * 40:
        page.keyboard.press(key)
        # the background is inert: focus is in the dialog, or has left the page for browser chrome
        ok = page.evaluate("(() => { const a = document.activeElement; return !a || a === document.body || !!a.closest('dialog.tf-dialog'); })()")
        assert ok, key
    inside = 0
    for _ in range(12):
        page.keyboard.press("Tab")
        inside += page.evaluate("!!document.activeElement.closest('dialog.tf-dialog')")
    assert inside >= 8


def test_backdrop_click_closes(page):
    open_new(page)
    page.mouse.click(4, 4)
    expect(dialog(page)).not_to_be_visible()


def test_drag_from_field_to_backdrop_does_not_close(page):
    open_new(page)
    box = page.locator(f"{DLG} .tf-name input").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 10)
    page.mouse.down()
    page.mouse.move(3, 3)
    page.mouse.up()
    expect(dialog(page)).to_be_visible()


def test_name_field_and_public_notice(page, shot):
    open_new(page)
    d = dialog(page)
    expect(d.locator(".tf-name")).to_be_visible()
    expect(d.locator(".tf-name")).to_contain_text("This name is public")
    expect(d.locator(".tf-name")).to_contain_text("Anyone with the link")
    assert f(row(page), "date").input_value() == TODAY
    shot(page, "01-new-trip")


def test_logged_in_hides_name_and_saves_without_panel(make_page, api):
    page = make_page(user=True)
    open_new(page)
    expect(dialog(page).locator(".tf-name")).to_be_hidden()
    type_flight(row(page), "dl1200")
    save(page)
    expect(dialog(page)).not_to_be_visible()
    s = stats(api)
    assert "name" not in s["last_post"] and "uid" not in s["last_post"]
    expect(page.locator("#trips li")).to_have_count(1)
    assert page.evaluate("window.__saved") == 1


# ---------------------------------------------------------------- rows
def test_kind_switch_carries_values(page):
    open_new(page)
    r = row(page)
    type_flight(r, "DL1200", **{"from": "jax", "to": "den"})
    r.get_by_role("button", name="Airports").click()
    expect(f(r, "flight_no")).to_be_hidden()
    assert f(r, "from").input_value() == "jax" and f(r, "to").input_value() == "den"
    assert f(r, "date").input_value() == TODAY
    f(r, "to").fill("ord")
    r.get_by_role("button", name="Flight number").click()
    expect(f(r, "flight_no")).to_be_visible()
    assert f(r, "flight_no").input_value() == "DL1200"
    assert f(r, "to").input_value() == "ord"
    assert r.get_by_role("button", name="Flight number").get_attribute("aria-pressed") == "true"


def test_mixed_rows_in_one_trip(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    page.click(".tf-add[data-dir=out]")
    r2 = row(page, 1)
    r2.get_by_role("button", name="Airports").click()
    f(r2, "from").fill("KDEN")
    f(r2, "to").fill("ORD")
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    out = stats(api)["last_post"]["out"]
    assert out[0]["flight_no"] == "DL1200" and "flight_no" not in out[1]
    assert out[1]["from"] == "KDEN" and out[1]["to"] == "ORD"


def test_add_and_remove_connection_rows(page):
    open_new(page)
    expect(page.get_by_role("button", name=re.compile(r"^Remove leg"))).to_have_count(0)
    page.click(".tf-add[data-dir=out]")
    rm = page.get_by_role("button", name="Remove leg 2")
    expect(rm).to_be_visible()
    box = rm.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44
    expect(page.get_by_role("button", name="Remove leg 1")).to_be_visible()
    rm.click()
    expect(page.locator(".tf-dir[data-dir=out] .tf-row")).to_have_count(1)
    assert page.evaluate("!!document.activeElement.closest('.tf-row')")


def test_max_rows(page):
    open_new(page)
    for _ in range(7):
        page.click(".tf-add[data-dir=out]")
    expect(page.locator(".tf-dir[data-dir=out] .tf-row")).to_have_count(8)
    expect(page.locator(".tf-add[data-dir=out]")).to_be_hidden()
    expect(page.locator(".tf-dir[data-dir=out] .tf-max")).to_be_visible()
    page.get_by_role("button", name="Remove leg 8").click()
    expect(page.locator(".tf-add[data-dir=out]")).to_be_visible()


def test_return_toggle_has_no_dead_state(page):
    open_new(page)
    toggle = page.locator(".tf-ret-toggle")
    expect(page.locator(".tf-dir[data-dir=ret]")).to_be_hidden()
    toggle.click()
    expect(page.locator(".tf-dir[data-dir=ret]")).to_be_visible()
    expect(toggle).to_have_text("Remove return")
    expect(page.locator(".tf-dir[data-dir=ret] .tf-row")).to_have_count(1)
    toggle.click()
    expect(page.locator(".tf-dir[data-dir=ret]")).to_be_hidden()
    expect(toggle).to_have_text("Add return")
    toggle.click()
    expect(page.locator(".tf-dir[data-dir=ret] .tf-row")).to_have_count(1)


def test_duplicate_rows_are_saved_once(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    page.click(".tf-add[data-dir=out]")
    type_flight(row(page, 1), "dl 1200")
    expect(row(page, 1).locator(".tf-dup")).to_have_text("Same as leg 1. I will only save it once.")
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    assert len(stats(api)["last_post"]["out"]) == 1


def test_empty_extra_row_ignored_half_filled_row_blocks(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    page.click(".tf-add[data-dir=out]")
    r2 = row(page, 1)
    f(r2, "from").fill("JAX")  # airports hint but no flight number
    save(page)
    alert = dialog(page).locator(".tf-alert")
    expect(alert).to_contain_text("Leg 2 needs a flight number")
    assert stats(api)["posts"] == 0
    assert page.evaluate("document.activeElement.dataset.field") == "flight_no"
    assert f(r2, "flight_no").get_attribute("aria-invalid") == "true"
    assert alert.get_attribute("role") == "alert"
    f(r2, "from").fill("")  # now completely empty: ignored
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    assert len(stats(api)["last_post"]["out"]) == 1


def test_nothing_to_save_and_missing_name(page, api):
    open_new(page)
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("Add at least one flight")
    type_flight(row(page), "DL1200")
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("Add your name")
    assert page.evaluate("document.activeElement.closest('.tf-name') !== null")
    assert stats(api)["posts"] == 0


def test_client_format_check_on_blur(page, api):
    open_new(page)
    r = row(page)
    f(r, "flight_no").fill("12")
    f(r, "date").focus()
    expect(r.locator(".field__error")).to_contain_text("does not look like a flight number")
    assert f(r, "flight_no").get_attribute("aria-invalid") == "true"
    page.wait_for_timeout(700)
    assert stats(api)["previews"] == 0  # bad format never reaches the server


# ---------------------------------------------------------------- live preview
def test_preview_ok_card(page, api, shot):
    open_new(page)
    r = row(page)
    type_flight(r, "dl1200")
    card = r.locator(".tf-card--ok")
    expect(card).to_contain_text("JAX to DEN, 8:15 to 10:05, Boeing 737-800")
    expect(card.locator(".status")).to_have_text("Verified")
    assert r.locator(".tf-result").get_attribute("role") == "status"
    log = stats(api)["preview_log"][-1]["rows"][0]
    assert log == {"kind": "flight", "flight_no": "DL1200", "date": TODAY}
    shot(page, "02-verified")


def test_preview_is_debounced_and_only_for_changed_rows(page, api):
    open_new(page)
    r = row(page)
    f(r, "flight_no").press_sequentially("DL1200", delay=40)
    expect(r.locator(".tf-card--ok")).to_be_visible()
    assert stats(api)["previews"] == 1
    page.click(".tf-add[data-dir=out]")
    type_flight(row(page, 1), "DL1201")
    expect(row(page, 1).locator(".tf-card--ok")).to_be_visible()
    f(row(page, 1), "flight_no").focus()
    f(row(page, 1), "date").focus()
    page.wait_for_timeout(700)
    assert stats(api)["previews"] == 2  # row 1 was not re-previewed


@pytest.mark.parametrize("flight,msg_start,field", [
    ("DL9999", "I couldn't find DL9999", "flight_no"),
    ("DL7777", "DL7777 isn't a flight number", "flight_no"),
    ("DL8888", "That date is outside the range", "date"),
])
def test_preview_errors_use_server_message(page, flight, msg_start, field):
    open_new(page)
    r = row(page)
    type_flight(r, flight)
    err = r.locator(".field__error")
    expect(err).to_contain_text(msg_start)
    inp = f(r, field)
    assert inp.get_attribute("aria-invalid") == "true"
    assert inp.get_attribute("aria-describedby") == err.get_attribute("id")


def test_airport_unknown_marks_both_airports(page):
    open_new(page)
    r = row(page)
    r.get_by_role("button", name="Airports").click()
    f(r, "from").fill("ZZZ")
    f(r, "to").fill("DEN")
    expect(r.locator(".field__error")).to_have_text("I don't know the airport ZZZ.")
    assert f(r, "from").get_attribute("aria-invalid") == "true"
    assert f(r, "to").get_attribute("aria-invalid") == "true"
    f(r, "from").fill("JAX")
    card = r.locator(".tf-card--ok")
    expect(card).to_contain_text("Jacksonville Intl to Denver Intl")
    expect(card.locator(".status")).to_have_text("Airports confirmed")
    assert f(r, "from").get_attribute("aria-invalid") is None


def test_editing_clears_a_stale_error(page):
    open_new(page)
    r = row(page)
    type_flight(r, "DL9999")
    expect(r.locator(".field__error")).to_be_visible()
    f(r, "flight_no").fill("DL1200")
    expect(r.locator(".tf-card--ok")).to_be_visible()
    expect(r.locator(".field__error")).to_have_count(0)
    assert f(r, "flight_no").get_attribute("aria-invalid") is None


def test_ambiguous_pick_sets_hints_and_repreviews(page, api, shot):
    open_new(page)
    r = row(page)
    type_flight(r, "AA100")
    group = r.locator(".tf-cands")
    expect(group).to_be_visible()
    radios = group.get_by_role("radio")
    expect(radios).to_have_count(2)
    expect(group).to_contain_text("JAX to ATL, 7:00 to 8:20")
    expect(group).to_contain_text("ATL to DEN, 11:10 to 12:40")
    shot(page, "03-ambiguous")
    group.get_by_role("radio", name="ATL to DEN, 11:10 to 12:40").click()
    expect(r.locator(".tf-card--ok")).to_contain_text("ATL to DEN")
    assert f(r, "from").input_value() == "KATL" and f(r, "to").input_value() == "KDEN"
    last = stats(api)["preview_log"][-1]["rows"][0]
    assert last["from"] == "KATL" and last["to"] == "KDEN"


def test_ambiguous_blocks_submit(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "AA100")
    expect(row(page).locator(".tf-cands")).to_be_visible()
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("pick which leg")
    assert stats(api)["posts"] == 0
    assert page.evaluate("document.activeElement.type") == "radio"


@pytest.mark.parametrize("flight", ["UA1", "BA1"])
def test_keep_it_anyway_flow(page, api, flight, shot):
    open_new(page)
    set_name(page)
    r = row(page)
    type_flight(r, flight)
    expect(r.locator(".field__error")).to_be_visible()
    keep = r.get_by_label("Keep it anyway")
    expect(keep).not_to_be_checked()
    if flight == "UA1":
        shot(page, "04-keep-anyway")
    keep.check()
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    post = stats(api)["last_post"]
    assert post["accept_unverified"] is True
    page.click(".tf-saved .btn--primary")
    expect(page.locator("#trips li")).to_contain_text("(unverified)")


def test_keep_anyway_must_be_ticked_for_every_unverifiable_row(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "UA1")
    page.click(".tf-add[data-dir=out]")
    type_flight(row(page, 1), "BA1")
    expect(row(page, 1).locator(".field__error")).to_be_visible()
    row(page, 0).get_by_label("Keep it anyway").check()
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("Tick Keep it anyway on leg 2")
    assert stats(api)["posts"] == 0


# ---------------------------------------------------------------- submit
def test_submit_busy_and_no_double_post(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL5555")
    expect(row(page).locator(".tf-card--ok")).to_be_visible()
    f(row(page), "flight_no").focus()
    save(page)
    btn = dialog(page).locator(".tf-save")
    expect(btn).to_be_disabled()
    assert dialog(page).locator("form.tf-form").get_attribute("aria-busy") == "true"
    expect(btn).to_have_text("Saving…")
    f(row(page), "flight_no").press("Enter")  # submit again while busy
    f(row(page), "flight_no").press("Enter")
    expect(page.locator(".tf-saved")).to_be_visible(timeout=5000)
    assert stats(api)["posts"] == 1


def test_400_rows_map_back_onto_rows(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    page.click(".tf-add[data-dir=out]")
    r2 = row(page, 1)
    f(r2, "flight_no").fill("UA1")
    f(r2, "flight_no").press("Enter")  # before the live check has run
    expect(r2.locator(".field__error")).to_contain_text("flight data service isn't answering")
    assert f(r2, "flight_no").get_attribute("aria-invalid") == "true"
    expect(row(page, 0).locator(".field__error")).to_have_count(0)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("Some legs need another look")
    assert page.evaluate("document.activeElement.id") == f(r2, "flight_no").get_attribute("id")
    # the user's entries survive, and keeping it fixes the save
    assert f(row(page, 0), "flight_no").input_value() == "DL1200"
    r2.get_by_label("Keep it anyway").check()
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    assert stats(api)["posts"] == 2


def test_429_keeps_form(page, api):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    page.route("**/api/trips", lambda r: r.fulfill(status=429, json={"detail": "Slow down"}) if r.request.method == "POST" else r.fallback())
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_contain_text("Try again in a few minutes")
    assert f(row(page), "flight_no").input_value() == "DL1200"
    expect(dialog(page).locator(".tf-save")).to_be_enabled()


def test_network_failure_keeps_form(page, api):
    open_new(page)
    set_name(page, "Kit")
    type_flight(row(page), "DL1200")
    page.route("**/api/trips", lambda r: r.abort() if r.request.method == "POST" else r.fallback())
    save(page)
    expect(dialog(page).locator(".tf-alert")).to_have_text("Couldn't reach the server. Your entries are still here.")
    assert f(row(page), "flight_no").input_value() == "DL1200"
    assert dialog(page).locator(".tf-name input").input_value() == "Kit"
    expect(dialog(page).locator(".tf-save")).to_be_enabled()
    page.unroute("**/api/trips")
    save(page)  # retry works without retyping
    expect(page.locator(".tf-saved")).to_be_visible()


def test_403_on_edit_explains_and_offers_refresh(page, api):
    r = api.post("/__seed", json={"out": [{"flight_no": "DL1200", "date": TODAY}], "name": "Sam"}).json()
    page.evaluate("""async ([id]) => { const m = await import('/static/js/identity.js'); m.rememberManage(id, 'wrongwrongwrongwrong1'); }""", [r["trip_id"]])
    page.reload()
    page.wait_for_selector("html[data-ready='1']")
    page.click("[data-edit]")
    expect(dialog(page)).to_be_visible()
    save(page)
    alert = dialog(page).locator(".tf-alert")
    expect(alert).to_contain_text("can no longer change that trip")
    before = page.evaluate("window.__saved")
    dialog(page).get_by_role("button", name="Refresh the board").click()
    expect(dialog(page)).not_to_be_visible()
    assert page.evaluate("window.__saved") == before + 1
    assert page.evaluate("localStorage.getItem('hft.manage')") in ("[]", None)


# ---------------------------------------------------------------- saved panel and manage link
def test_saved_panel_and_fragment_link_round_trip(page, api, browser, shot):
    open_new(page)
    set_name(page, "Robin")
    type_flight(row(page), "DL1200")
    save(page)
    panel = page.locator(".tf-saved")
    expect(panel).to_be_visible()
    assert page.evaluate("document.activeElement.textContent") == "Trip saved"
    assert dialog(page).get_attribute("aria-labelledby") == page.locator(".tf-saved__title").get_attribute("id")
    link = panel.locator(".tf-link").input_value()
    m = re.fullmatch(re.escape(page.base) + r"/#manage=(\d+)\.([A-Za-z0-9_-]{32})", link)
    assert m, link
    assert "?" not in link
    expect(panel).to_contain_text("Anyone who has it can change this trip")
    panel.get_by_role("button", name="Copy").click()
    expect(panel.get_by_role("status")).to_have_text("Copied.")
    assert page.evaluate("navigator.clipboard.readText()") == link
    shot(page, "05-saved")
    assert page.evaluate("window.__saved") == 1
    # identity and token were remembered in this browser
    ident = json.loads(page.evaluate("localStorage.getItem('hft.identity')"))
    assert ident["name"] == "Robin" and ident["uid"].startswith("m_")
    panel.get_by_role("button", name="Done").click()
    expect(dialog(page)).not_to_be_visible()
    expect(page.locator("[data-edit]")).to_have_count(1)

    # a fresh browser context has no tokens; the link grants them and strips itself
    ctx2 = browser.new_context(viewport={"width": 1000, "height": 800}, timezone_id="UTC")
    try:
        p2 = ctx2.new_page()
        p2.goto(link)
        p2.wait_for_selector("html[data-ready='1']")
        assert p2.evaluate("window.__seed") == {"tripId": m.group(1)}
        assert p2.evaluate("location.hash") == ""
        assert p2.evaluate("location.href") == page.base + "/"
        expect(p2.locator("[data-edit]")).to_have_count(1)
        p2.click("[data-edit]")
        f(p2.locator(".tf-dir[data-dir=out] .tf-row").first, "flight_no").fill("DL1201")
        p2.locator(f"{DLG} .tf-save").click()
        expect(p2.locator(DLG).first).not_to_be_visible()
        s = stats(api)
        assert s["puts"] == 1
        assert s["last_put_headers"]["x-manage-token"] == m.group(2)
        assert "name" not in s["last_put"]
    finally:
        ctx2.close()


def test_copy_failure_is_reported_honestly(page):
    open_new(page)
    set_name(page)
    type_flight(row(page), "DL1200")
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    page.evaluate("navigator.clipboard.writeText = () => Promise.reject(new Error('no')); document.execCommand = () => false;")
    page.locator(".tf-saved").get_by_role("button", name="Copy").click()
    expect(page.locator(".tf-saved").get_by_role("status")).to_contain_text("Couldn't copy it automatically")


def test_proof_and_uid_sent_on_second_trip(page, api):
    open_new(page)
    set_name(page, "Robin")
    type_flight(row(page), "DL1200")
    save(page)
    page.locator(".tf-saved").get_by_role("button", name="Done").click()
    first_tok = json.loads(page.evaluate("localStorage.getItem('hft.manage')"))[0]["token"]
    open_new(page)
    assert dialog(page).locator(".tf-name input").input_value() == "Robin"
    type_flight(row(page), "DL1201")
    save(page)
    expect(page.locator(".tf-saved")).to_be_visible()
    post = stats(api)["last_post"]
    assert post["proof"] == first_tok and post["uid"].startswith("m_")


# ---------------------------------------------------------------- edit and remove
def seed_mixed(page, api):
    return seed_and_remember(
        page, api,
        out=[{"flight_no": "DL1200", "date": "2026-10-10"}],
        ret=[{"from": "KDEN", "to": "KJAX", "date": "2026-10-12"}],
    )


def test_edit_prefills_kinds_and_saves_with_token(page, api, shot):
    seeded = seed_mixed(page, api)
    page.click("[data-edit]")
    d = dialog(page)
    expect(d.locator(".tf-title").first).to_have_text("Edit trip")
    expect(d.locator(".tf-name")).to_be_hidden()
    r0, r1 = row(page, 0), row(page, 0, "ret")
    assert r0.get_attribute("data-kind") == "flight" and r1.get_attribute("data-kind") == "manual"
    assert f(r0, "flight_no").input_value() == "DL1200" and f(r0, "date").input_value() == "2026-10-10"
    assert f(r0, "from").input_value() == "JAX" and f(r0, "to").input_value() == "DEN"
    assert f(r1, "from").input_value() == "DEN" and f(r1, "to").input_value() == "JAX"
    expect(page.locator(".tf-ret-toggle")).to_have_text("Remove return")
    expect(d.locator(".tf-save")).to_have_text("Save changes")
    shot(page, "06-edit-mixed")
    f(r1, "to").fill("ORD")
    d.locator(".tf-save").click()
    expect(d).not_to_be_visible()
    s = stats(api)
    assert s["last_put_headers"]["x-manage-token"] == seeded["token"]
    assert s["last_put"]["ret"] == [{"from": "DEN", "to": "ORD", "date": "2026-10-12"}]
    assert s["last_put"]["out"][0]["flight_no"] == "DL1200"
    assert "name" not in s["last_put"]


def test_edit_of_missing_trip_says_so(page, api):
    seeded = seed_mixed(page, api)
    page.evaluate("window.__form.openEdit(9999)")
    expect(dialog(page).locator(".tf-title").first).not_to_be_visible()
    notice = page.locator(DLG).nth(1)
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("no longer on the board")
    page.keyboard.press("Escape")
    assert seeded["trip_id"] == 1


def test_remove_flow(page, api, shot):
    seed_mixed(page, api)
    page.click("[data-remove]")
    rm = page.locator(DLG).nth(1)
    expect(rm).to_be_visible()
    expect(rm.locator(".tf-title")).to_have_text("Remove JAX to DEN on Oct 10?")
    expect(rm).to_contain_text("all 2 legs")
    assert page.evaluate("document.activeElement.textContent") == "Cancel"
    shot(page, "07-remove")
    rm.get_by_role("button", name="Cancel").click()
    expect(rm).not_to_be_visible()
    assert stats(api)["deletes"] == 0
    page.click("[data-remove]")
    rm.get_by_role("button", name="Remove", exact=True).click()
    expect(rm).not_to_be_visible()
    expect(page.locator("#trips li")).to_have_count(0)
    assert stats(api)["deletes"] == 1
    assert page.evaluate("localStorage.getItem('hft.manage')") in ("[]", None)


def test_remove_404_means_already_removed(page, api):
    seed_mixed(page, api)
    page.click("[data-remove]")
    api.post("/__drop/1")
    before = page.evaluate("window.__saved")
    page.locator(DLG).nth(1).get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator(DLG).nth(1)).not_to_be_visible()
    assert page.evaluate("window.__saved") == before + 1
    assert page.evaluate("localStorage.getItem('hft.manage')") in ("[]", None)


def test_remove_403_shows_inline_error(page, api):
    r = api.post("/__seed", json={"out": [{"flight_no": "DL1200", "date": TODAY}]}).json()
    page.evaluate("""async ([id]) => { const m = await import('/static/js/identity.js'); m.rememberManage(id, 'wrongwrongwrongwrong1'); }""", [r["trip_id"]])
    page.reload()
    page.wait_for_selector("html[data-ready='1']")
    page.click("[data-remove]")
    rm = page.locator(DLG).nth(1)
    rm.get_by_role("button", name="Remove", exact=True).click()
    expect(rm.locator(".tf-alert")).to_contain_text("can no longer remove that trip")
    expect(rm).to_be_visible()
    expect(rm.get_by_role("button", name="Remove", exact=True)).to_be_enabled()


# ---------------------------------------------------------------- layout and keyboard
def no_horizontal_overflow(page):
    return page.evaluate("""() => {
      const d = document.querySelector('dialog.tf-dialog');
      const vw = document.documentElement.clientWidth;
      const over = [...d.querySelectorAll('*')].filter(e => e.offsetParent && e.getBoundingClientRect().right > vw + 0.5 && !e.closest('[hidden]'));
      return {page: document.documentElement.scrollWidth <= vw, dialog: d.scrollWidth <= d.clientWidth, over: over.map(e => e.className).slice(0, 5)};
    }""")


def test_mobile_390_layout(make_page, shot):
    page = make_page(390, 844)
    open_new(page)
    page.click(".tf-ret-toggle")
    page.click(".tf-add[data-dir=out]")
    type_flight(row(page, 0), "DL1200")
    expect(row(page, 0).locator(".tf-card--ok")).to_be_visible()
    type_flight(row(page, 1), "AA100")
    expect(row(page, 1).locator(".tf-cands")).to_be_visible()
    row(page, 0, "ret").get_by_role("button", name="Airports").click()
    res = no_horizontal_overflow(page)
    assert res["page"] and res["dialog"] and not res["over"], res
    # flight and date share a line, From and To share the next
    a = f(row(page, 0), "flight_no").bounding_box()
    b = f(row(page, 0), "date").bounding_box()
    c = f(row(page, 0), "from").bounding_box()
    e = f(row(page, 0), "to").bounding_box()
    assert abs(a["y"] - b["y"]) < 2 and abs(c["y"] - e["y"]) < 2 and c["y"] > a["y"] + a["height"]
    # actions bar stays on screen while the body scrolls
    save_box = dialog(page).locator(".tf-save").bounding_box()
    assert save_box["y"] + save_box["height"] <= 844 and save_box["height"] >= 44
    shot(page, "08-mobile-390")
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    shot(page, "09-mobile-390-light")


def test_desktop_light_theme_screenshot(page, shot):
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    open_new(page)
    type_flight(row(page), "DL9999")
    expect(row(page).locator(".field__error")).to_be_visible()
    shot(page, "10-desktop-light-error")


def test_keyboard_only_completion(page, api):
    page.focus("#openAdd")
    page.keyboard.press("Enter")
    expect(dialog(page)).to_be_visible()
    page.keyboard.type("Quinn")          # name field has focus
    page.keyboard.press("Tab")           # Flight number segment
    page.keyboard.press("Tab")           # Airports segment
    page.keyboard.press("Tab")           # flight number input
    assert page.evaluate("document.activeElement.dataset.field") == "flight_no"
    page.keyboard.type("dl1200")
    expect(row(page).locator(".tf-card--ok")).to_be_visible()
    page.keyboard.press("Enter")         # submits from a field
    expect(page.locator(".tf-saved")).to_be_visible()
    assert page.evaluate("document.activeElement.textContent") == "Trip saved"
    page.keyboard.press("Tab")           # close button
    page.keyboard.press("Tab")           # link
    page.keyboard.press("Tab")           # copy
    page.keyboard.press("Enter")
    expect(page.locator(".tf-saved").get_by_role("status")).to_have_text("Copied.")
    page.keyboard.press("Tab")           # done
    page.keyboard.press("Enter")
    expect(dialog(page)).not_to_be_visible()
    assert page.evaluate("document.activeElement.id") == "openAdd"
    assert stats(api)["posts"] == 1


# ---------------------------------------------------------------- identity.js
IMPORT = "const m = await import('/static/js/identity.js?t=' + Math.random());"


def ev(page, body, arg=None):
    return page.evaluate(f"async (arg) => {{ {IMPORT} {body} }}", arg)


def test_identity_round_trip_and_order(page):
    r = ev(page, """
      m.saveIdentity('m_abc', 'Sam');
      const i = m.identity();
      m.rememberManage(1, arg.a); m.rememberManage(2, arg.b); m.rememberManage(1, arg.c);
      const afterThree = m.proofToken();
      m.forgetManage(1);
      return {i, afterThree, afterForget: m.proofToken(), t1: m.manageToken(1), t2: m.manageToken('2'),
              bad: m.rememberManage('x', arg.a), bad2: m.rememberManage(5, 'short')};
    """, {"a": TOK_A, "b": TOK_B, "c": TOK_C})
    assert r["i"] == {"uid": "m_abc", "name": "Sam"}
    assert r["afterThree"] == TOK_C and r["afterForget"] == TOK_B
    assert r["t1"] is None and r["t2"] == TOK_B
    assert r["bad"] is False and r["bad2"] is False


def test_identity_default_when_empty(page):
    assert ev(page, "localStorage.clear(); return m.identity();") == {"uid": None, "name": ""}


def test_identity_migrates_legacy_object_map(page):
    r = ev(page, """
      localStorage.setItem('hft.manage', JSON.stringify({'5': arg.a, '9': arg.b, 'nope': arg.c}));
      const out = {t5: m.manageToken(5), t9: m.manageToken(9), proof: m.proofToken()};
      out.stored = JSON.parse(localStorage.getItem('hft.manage'));
      return out;
    """, {"a": TOK_A, "b": TOK_B, "c": TOK_C})
    assert r["t5"] == TOK_A and r["t9"] == TOK_B and r["proof"] == TOK_B
    assert isinstance(r["stored"], list) and [e["tripId"] for e in r["stored"]] == ["5", "9"]
    assert all(set(e) == {"tripId", "token", "at"} for e in r["stored"])


def test_identity_prune_and_proof_prefers_live(page):
    r = ev(page, """
      const now = Date.now();
      localStorage.setItem('hft.manage', JSON.stringify([
        {tripId: '1', token: arg.a, at: 1000},
        {tripId: '2', token: arg.b, at: 2000},
        {tripId: '3', token: arg.c, at: 3000},
        {tripId: '4', token: arg.a + 'x', at: now},
      ]));
      const before = m.proofToken();
      const dropped = m.pruneManage(new Set([1, 2]));
      return {before, dropped, t3: m.manageToken(3), t4: m.manageToken(4), t2: m.manageToken(2), proof: m.proofToken()};
    """, {"a": TOK_A, "b": TOK_B, "c": TOK_C})
    assert r["before"] == TOK_A + "x"            # newest overall when nothing is known to be live
    assert r["dropped"] == 1 and r["t3"] is None  # stale and not live: dropped
    assert r["t4"] == TOK_A + "x"                 # just stored: protected from a racing poll
    assert r["t2"] == TOK_B
    assert r["proof"] == TOK_B                    # live trip preferred over the newer unconfirmed one


def test_identity_can_manage(page):
    r = ev(page, """
      m.rememberManage(7, arg.a);
      return [m.canManage('o1', 7, {me: null, isAdmin: false}), m.canManage('o1', 8, {me: null, isAdmin: false}),
              m.canManage('o1', 8, {me: 'o1', isAdmin: false}), m.canManage('o1', 8, {me: 'o2', isAdmin: false}),
              m.canManage('o1', 8, {me: null, isAdmin: true}), m.canManage('o1', 8)];
    """, {"a": TOK_A})
    assert r == [True, False, True, False, True, False]


def test_identity_seed_from_fragment(page):
    r = ev(page, """
      const out = {};
      history.replaceState(null, '', '/?x=1#manage=7.' + arg.a);
      out.ok = m.seedFromFragment(); out.url = location.pathname + location.search + location.hash; out.tok = m.manageToken(7);
      history.replaceState(null, '', '/#manage=abc.' + arg.a);
      out.badId = m.seedFromFragment(); out.badIdUrl = location.pathname + location.search + location.hash;
      history.replaceState(null, '', '/#manage=8.short');
      out.badTok = m.seedFromFragment(); out.t8 = m.manageToken(8);
      history.replaceState(null, '', '/#manage=9.' + arg.b + '!');
      out.badChar = m.seedFromFragment(); out.t9 = m.manageToken(9);
      history.replaceState(null, '', '/?manage=11.' + arg.b + '&keep=1');
      out.legacy = m.seedFromFragment(); out.legacyUrl = location.pathname + location.search + location.hash; out.t11 = m.manageToken(11);
      history.replaceState(null, '', '/#other');
      out.none = m.seedFromFragment(); out.otherUrl = location.hash;
      return out;
    """, {"a": TOK_A, "b": TOK_B})
    assert r["ok"] == {"tripId": "7"} and r["url"] == "/?x=1" and r["tok"] == TOK_A
    assert r["badId"] is None and r["badIdUrl"] == "/"
    assert r["badTok"] is None and r["t8"] is None
    assert r["badChar"] is None and r["t9"] is None
    assert r["legacy"] == {"tripId": "11"} and r["legacyUrl"] == "/?keep=1" and r["t11"] == TOK_B
    assert r["none"] is None and r["otherUrl"] == "#other"


def test_identity_works_when_storage_throws(browser, server):
    ctx = browser.new_context()
    try:
        ctx.add_init_script("""
          Storage.prototype.getItem = function () { throw new Error('blocked'); };
          Storage.prototype.setItem = function () { throw new Error('blocked'); };
        """)
        p = ctx.new_page()
        p.goto(server.url + "/")
        p.wait_for_selector("html[data-ready='1']")
        r = ev(p, """
          m.saveIdentity('m_q', 'Q'); m.rememberManage(3, arg.a);
          return {i: m.identity(), t: m.manageToken(3), proof: m.proofToken()};
        """, {"a": TOK_A})
        assert r == {"i": {"uid": "m_q", "name": "Q"}, "t": TOK_A, "proof": TOK_A}
    finally:
        ctx.close()
