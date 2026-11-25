from __future__ import annotations

from shared.models import DocState, DocStatus, ParseRoute


def test_unknown_parse_route_does_not_raise() -> None:
    doc = DocState.from_firestore(
        {"fileId": "f1", "driveId": "d", "parseRoute": "FUTURE_ENGINE"}
    )
    assert doc.parse_route == ParseRoute.NONE
    assert doc.file_id == "f1"


def test_unknown_status_falls_back_to_pending() -> None:
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
