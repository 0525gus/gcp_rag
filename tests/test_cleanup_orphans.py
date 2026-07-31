"""GCS 잔존 객체 판정 규칙.

삭제하는 스크립트라 판정이 틀리면 살아 있는 문서의 산출물을 지운다.
GCP 의존이 없는 `classify` 만 대상으로 규칙을 못 박는다.
"""

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
    for name in ("normalized/live1.md", "normalized/live1.meta.md", "raw/live1.hwp"):
        assert classify(name, _LIVE) is None


def test_keeps_failed_documents_because_retry_will_pick_them_up() -> None:
    # /sync/retry-failed 가 다시 집어가므로 산출물을 남겨야 한다
    assert classify("raw/failed1.hwp", _LIVE) is None
    assert classify("normalized/failed1.md", _LIVE) is None


def test_flags_raw_original_of_deleted_document() -> None:
    """이 스크립트가 존재하는 이유 — 실측 52건이 이 경우였다."""
    verdict = classify("raw/gone1.hwp", _LIVE)
    assert verdict == ("gone1", REASON_DELETED)


def test_flags_deleted_document_across_all_produced_extensions() -> None:
    for name in (
        "raw/gone1.hwp",
        "raw/gone1.hwpx",
        "normalized/gone1.md",
        "normalized/gone1.meta.md",
        "normalized/gone1.part3.pdf",  # 분할 조각 — 옛 삭제 목록이 놓쳤다
        "normalized/gone1.rtf",  # 옛 삭제 목록에 없던 확장자
        "normalized/gone1.doc",
    ):
        assert classify(name, _LIVE) == ("gone1", REASON_DELETED), name


def test_unknown_status_is_flagged_but_can_be_excluded() -> None:
    assert classify("raw/nosuchid.hwp", _LIVE) == ("nosuchid", REASON_UNKNOWN)
    # doc_state 를 유실한 뒤라면 살아 있는 문서일 수 있으므로 뺄 수 있어야 한다
    assert classify("raw/nosuchid.hwp", _LIVE, only_deleted=True) is None


def test_ignores_blobs_without_a_recoverable_file_id() -> None:
    assert classify("raw/", _LIVE) is None
