from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.sync.main as sync_main
from services.sync.main import IngestBody, _resolve_audience
from settings_presets import NO_LEASES_OR_MAPPINGS
from shared.config import Settings
from shared.models import Audience, DocState, DocStatus

STUDENT_FOLDER = "1EZAc-B14FvfF9AyU4wKKEOxuS0z77WBS"
STAFF_FOLDER = "1OHYYDO3YpuOc7OyGWi7wZ_br_zVgtDQG"


def _settings(**over) -> Settings:
    base = {
        "gcp_project_id": "p",
        "rag_corpus_name": "corpus/staff",
        "rag_corpus_name_student": "corpus/student",
        "sync_folder_ids": f"{STUDENT_FOLDER},{STAFF_FOLDER}",
        "student_folder_ids": STUDENT_FOLDER,
        **NO_LEASES_OR_MAPPINGS,
    }
    base.update(over)
    return Settings(**base)


def _body(file_id: str = "fileAAAAAAAAAA") -> IngestBody:
    return IngestBody(fileId=file_id, driveId="d")


class _Drive:
    def __init__(self, in_scope: bool | Exception) -> None:
        self._in_scope = in_scope

    def is_in_sync_scope(self, file_id, folder_ids, **kw):
        if isinstance(self._in_scope, Exception):
            raise self._in_scope
        return self._in_scope


def test_student_folder_yields_student() -> None:
    got = _resolve_audience(_Drive(True), _settings(), _body())
    assert got is Audience.STUDENT


def test_outside_student_folder_yields_staff() -> None:
    got = _resolve_audience(_Drive(False), _settings(), _body())
    assert got is Audience.STAFF


def test_drive_failure_falls_back_to_staff() -> None:
    got = _resolve_audience(_Drive(RuntimeError("drive down")), _settings(), _body())
    assert got is Audience.STAFF


def test_no_student_folder_configured_yields_staff() -> None:
    got = _resolve_audience(_Drive(True), _settings(student_folder_ids=""), _body())
    assert got is Audience.STAFF


def test_split_requires_both_corpus_and_folders() -> None:
    assert _settings().audience_split_enabled is True
    assert _settings(rag_corpus_name_student="").audience_split_enabled is False
    assert _settings(student_folder_ids="").audience_split_enabled is False


def test_legacy_doc_without_audience_reads_as_staff() -> None:
    state = DocState.from_firestore({"fileId": "f1", "driveId": "d"})
    assert state.audience is Audience.STAFF


def test_unknown_audience_value_reads_as_staff() -> None:
    state = DocState.from_firestore(
        {"fileId": "f1", "driveId": "d", "audience": "EVERYONE"}
    )
    assert state.audience is Audience.STAFF


def test_audience_round_trips_through_firestore() -> None:
    src = DocState(file_id="f1", drive_id="d", audience=Audience.STUDENT)
    assert DocState.from_firestore(src.to_firestore()).audience is Audience.STUDENT


class _Rag:
    calls: ClassVar[list[tuple[str, str, list[str]]]] = []

    def __init__(self, settings=None, *, corpus_name=None) -> None:
        self.corpus_name = corpus_name or "corpus/staff"

    def delete_files_by_ids(self, ids) -> int:
        _Rag.calls.append(("delete", self.corpus_name, list(ids)))
        return len(ids)

    def import_from_gcs(self, uris):
        from shared.rag_engine import ImportOutcome

        _Rag.calls.append(("import", self.corpus_name, list(uris)))
        return ImportOutcome(
            uris=list(uris), imported=len(uris), failed=0, skipped=0
        )


class _Store:
    def __init__(self, audiences: dict[str, Audience]) -> None:
        self._a = audiences

    def get(self, file_id: str) -> DocState | None:
        if file_id not in self._a:
            return None
        return DocState(
            file_id=file_id,
            drive_id="d",
            status=DocStatus.INDEXED,
            audience=self._a[file_id],
        )


def _run_student_sync(monkeypatch, uris, audiences, settings=None):
    _Rag.calls = []
    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    return sync_main._sync_student_corpus(
        uris, [], settings or _settings(), _Store(audiences)
    )


def test_only_student_docs_are_imported(monkeypatch) -> None:
    res = _run_student_sync(
        monkeypatch,
        ["gs://n/studentAAAAAA.md", "gs://n/staffBBBBBBB.md"],
        {"studentAAAAAA": Audience.STUDENT, "staffBBBBBBB": Audience.STAFF},
    )
    imports = [c for c in _Rag.calls if c[0] == "import"]
    assert res["imported"] == 1
    assert imports == [("import", "corpus/student", ["gs://n/studentAAAAAA.md"])]


def test_moved_out_doc_is_deleted_from_student_corpus(monkeypatch) -> None:
    _run_student_sync(
        monkeypatch,
        ["gs://n/movedCCCCCCC.md"],
        {"movedCCCCCCC": Audience.STAFF},
    )
    deletes = [c for c in _Rag.calls if c[0] == "delete"]
    assert deletes == [("delete", "corpus/student", ["movedCCCCCCC"])]
    assert not [c for c in _Rag.calls if c[0] == "import"]


def test_unknown_doc_is_not_given_to_students(monkeypatch) -> None:
    res = _run_student_sync(monkeypatch, ["gs://n/ghostDDDDDDD.md"], {})
    assert res["imported"] == 0


def test_sidecar_and_body_share_one_file_id(monkeypatch) -> None:
    _run_student_sync(
        monkeypatch,
        ["gs://n/pdfEEEEEEEEE.pdf", "gs://n/pdfEEEEEEEEE.meta.md"],
        {"pdfEEEEEEEEE": Audience.STUDENT},
    )
    imports = [c for c in _Rag.calls if c[0] == "import"]
    deletes = [c for c in _Rag.calls if c[0] == "delete"]
    assert len(imports[0][2]) == 2
    assert deletes[0][2] == ["pdfEEEEEEEEE"]


def test_disabled_split_touches_nothing(monkeypatch) -> None:
    res = _run_student_sync(
        monkeypatch,
        ["gs://n/studentAAAAAA.md"],
        {"studentAAAAAA": Audience.STUDENT},
        settings=_settings(rag_corpus_name_student=""),
    )
    assert res == {"enabled": False}
    assert _Rag.calls == []
