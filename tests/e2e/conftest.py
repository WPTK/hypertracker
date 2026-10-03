"""Everything under tests/e2e is a browser test: mark it so CI can select it with
`-m e2e` and the fast job can exclude it with `-m "not e2e"`."""

import os
from pathlib import Path

import pytest

_E2E_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _E2E_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def browser():
    """One Chromium for the whole session. Playwright's sync API can only be
    started once per process, so every e2e suite shares this fixture."""
    import glob

    sync_api = pytest.importorskip("playwright.sync_api")
    # Only point Playwright at the sandbox browser folder when it exists. Setting it
    # unconditionally made CI look in the wrong place and silently skip every test.
    if os.path.isdir("/opt/pw-browsers"):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    candidates = [os.environ["CHROMIUM_PATH"]] if os.environ.get("CHROMIUM_PATH") else []
    candidates += sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    candidates += sorted(glob.glob("/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell"))
    with sync_api.sync_playwright() as pw:
        browser = None
        for exe in [*candidates, None]:  # None = Playwright's own default lookup
            try:
                browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                break
            except Exception:  # noqa: BLE001, S112 - try the next candidate
                continue
        if browser is None:
            if os.environ.get("CI"):
                pytest.fail("Chromium is not available in CI; run `playwright install chromium`")
            pytest.skip("Chromium is not available")
        yield browser
        browser.close()


# CI sets E2E_FAIL_ON_SKIP so a browser suite that quietly skips can never look green.
_skipped: list[str] = []


def pytest_runtest_logreport(report):
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_collectreport(report):
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("E2E_FAIL_ON_SKIP") and _skipped and exitstatus == 0:
        print(f"\nE2E_FAIL_ON_SKIP: {len(_skipped)} browser tests were skipped; treating that as a failure.")
        session.exitstatus = 1
