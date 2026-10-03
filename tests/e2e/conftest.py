"""Everything under tests/e2e is a browser test: mark it so CI can select it with
`-m e2e` and the fast job can exclude it with `-m "not e2e"`."""

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
    import os

    sync_api = pytest.importorskip("playwright.sync_api")
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
            pytest.skip("Chromium is not available")
        yield browser
        browser.close()
