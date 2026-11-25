from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cleanup_orphans import (  # noqa: E402
    REASON_DELETED,
    REASON_UNKNOWN,
    classify,
)

_LIVE = {"live1": "INDEXED", "parsed1": "PARSED", "failed1": "FAILED",
         "skipped1": "SKIPPED", "gone1": "DELETED"}


def test_keeps_objects_of_live_documents() -> None:
    for name in ("live1.md", "live1.meta.md", "live1.hwp"):
        assert classify(name, _LIVE) is None


def test_keeps_failed_documents_because_retry_will_pick_them_up() -> None:
    assert classify("failed1.hwp", _LIVE) is None
    assert classify("failed1.md", _LIVE) is None


def test_flags_raw_original_of_deleted_document() -> None:
    verdict = classify("gone1.hwp", _LIVE)
    assert verdict == ("gone1", REASON_DELETED)


def test_flags_deleted_document_across_all_produced_extensions() -> None:
    for name in (
        "gone1.hwp",
        "gone1.hwpx",
        "gone1.md",
        "gone1.meta.md",
        "gone1.part3.pdf",
        "gone1.rtf",
        "gone1.doc",
    ):
        assert classify(name, _LIVE) == ("gone1", REASON_DELETED), name


def test_unknown_status_is_flagged_but_can_be_excluded() -> None:
    assert classify("nosuchid.hwp", _LIVE) == ("nosuchid", REASON_UNKNOWN)
    assert classify("nosuchid.hwp", _LIVE, only_deleted=True) is None


def test_ignores_blobs_without_a_recoverable_file_id() -> None:
    assert classify("", _LIVE) is None
