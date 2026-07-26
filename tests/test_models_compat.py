"""서비스별 배포 시차에 대한 Firestore 역직렬화 내성.

parser·sync·mcp 가 각각 배포되므로, 한쪽이 새 enum 값을 쓰기 시작한 뒤 구버전인
다른 쪽이 같은 문서를 읽는 시간대가 생긴다. 그때 죽지 않아야 한다.
"""

from __future__ import annotations

from shared.models import DocState, DocStatus, ParseRoute


def test_unknown_parse_route_does_not_raise() -> None:
    # 예: 신버전 parser 가 기록한 route 를 구버전 mcp 가 읽는 상황
    doc = DocState.from_firestore(
        {"fileId": "f1", "driveId": "d", "parseRoute": "FUTURE_ENGINE"}
    )
    assert doc.parse_route == ParseRoute.NONE
    assert doc.file_id == "f1"


def test_unknown_status_falls_back_to_pending() -> None:
    # 스킵(INDEXED)이 아니라 재처리(PENDING) 쪽으로 실패해야 안전하다
    doc = DocState.from_firestore(
        {"fileId": "f1", "driveId": "d", "status": "FUTURE_STATUS"}
    )
    assert doc.status == DocStatus.PENDING


def test_known_values_still_parse() -> None:
    doc = DocState.from_firestore(
        {"fileId": "f1", "driveId": "d", "status": "INDEXED", "parseRoute": "HWPX"}
    )
    assert doc.status == DocStatus.INDEXED
    assert doc.parse_route == ParseRoute.HWPX


def test_missing_fields_use_defaults() -> None:
    doc = DocState.from_firestore({"fileId": "f1", "driveId": "d"})
    assert doc.status == DocStatus.PENDING
    assert doc.parse_route == ParseRoute.NONE


def test_roundtrip_preserves_enum_values() -> None:
    original = DocState(
        file_id="f1", drive_id="d", status=DocStatus.INDEXED, parse_route=ParseRoute.HWPX
    )
    restored = DocState.from_firestore(original.to_firestore())
    assert restored.status == DocStatus.INDEXED
    assert restored.parse_route == ParseRoute.HWPX
