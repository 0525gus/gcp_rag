from __future__ import annotations

import pytest

from scripts import gcp_cli


@pytest.fixture(autouse=True)
def _fresh_gcloud_access_token():
    gcp_cli.forget_access_token()
    yield
