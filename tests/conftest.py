from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _fresh_gcloud_access_token():
    gui = sys.modules.get("scripts.dept_gui")
    if gui is not None and hasattr(gui, "_forget_access_token"):
        gui._forget_access_token()
    yield
