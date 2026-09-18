import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
SNAPSHOT = ROOT / "tests" / "frozen" / "golden200-2026-09-13"


def test_golden200_snapshot_is_frozen_and_bundle_grouped():
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text(encoding="utf-8"))
    dev = json.loads((SNAPSHOT / "golden200_dev150.json").read_text(encoding="utf-8"))
    holdout = json.loads((SNAPSHOT / "golden200_holdout50.json").read_text(encoding="utf-8"))

    assert manifest["snapshot_id"] == "golden200-2026-09-13"
    assert manifest["seed"] == 20260913
    assert len(dev) == 150
    assert len(holdout) == 50
    assert {row["n"] for row in dev}.isdisjoint({row["n"] for row in holdout})
    assert manifest["dev_case_numbers"] == [row["n"] for row in dev]
    assert manifest["holdout_case_numbers"] == [row["n"] for row in holdout]
    for name, digest in manifest["files"].items():
        actual = hashlib.sha256((SNAPSHOT / name).read_bytes()).hexdigest()
        assert digest == f"sha256:{actual}"
