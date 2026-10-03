"""Everything under tests/e2e is a browser test: mark it so CI can select it with
`-m e2e` and the fast job can exclude it with `-m "not e2e"`."""

from pathlib import Path

import pytest

_E2E_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _E2E_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)
