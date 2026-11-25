from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings_presets import NO_LEASES_OR_MAPPINGS
from shared.config import Department, Settings, UnknownDriveError, _departments_from_json
from shared.rag_engine import ImportOutcome

BASE = {
    "gcp_project_id": "p",
    "gcs_hwp_original_bucket": "shared-hwp",
    "gcs_source_bucket": "shared-src",
    "rag_corpus_name": "corpus-default",
    "rag_corpus_name_student": "corpus-default-student",
    "sync_folder_ids": "SHARED_FOLDER",
    **NO_LEASES_OR_MAPPINGS,
}

CS = Department(
    code="cs",
    drive_ids=("D_CS",),
    staff_corpus="c-cs-staff",
    student_corpus="c-cs-student",
    hwp_bucket="b-cs-hwp",
    source_bucket="b-cs-src",
    student_folder_ids=("F_STU",),
    sync_folder_ids=("F_A", "F_B"),
)
EE = Department(code="ee", drive_ids=("D_EE",), staff_corpus="c-ee-staff",
                hwp_bucket="shared-hwp", source_bucket="shared-src",
                sync_folder_ids=("SHARED_FOLDER",))


def test_empty_map_keeps_current_behaviour():
    s = Settings(**BASE)
    assert s.for_drive("anything") is s
    assert s.department_for_drive("anything") is None


def test_optional_student_fields_do_not_inherit_shared_values():
    s = Settings(**BASE, departments=(CS, EE))
    ee = s.for_drive("D_EE")
    assert ee.rag_corpus_name == "c-ee-staff"
    assert ee.rag_corpus_name_student == ""
    assert ee.student_folder_ids == ""
    assert ee.gcs_source_bucket == "shared-src"
    assert ee.sync_folder_ids == "SHARED_FOLDER"


def test_drive_selects_department_values():
    cs = Settings(**BASE, departments=(CS, EE)).for_drive("D_CS")
    assert cs.rag_corpus_name == "c-cs-staff"
    assert cs.rag_corpus_name_student == "c-cs-student"
    assert cs.gcs_hwp_original_bucket == "b-cs-hwp"
    assert cs.gcs_source_bucket == "b-cs-src"
    assert cs.student_folder_id_list == ["F_STU"]
    assert cs.sync_folder_id_list == ["F_A", "F_B"]


def test_departments_do_not_bleed_into_each_other():
    s = Settings(**BASE, departments=(CS, EE))
    assert s.for_drive("D_CS").rag_corpus_name != s.for_drive("D_EE").rag_corpus_name
    assert s.rag_corpus_name == "corpus-default"


def test_unknown_drive_raises_instead_of_defaulting():
    s = Settings(**BASE, departments=(CS,))
    with pytest.raises(UnknownDriveError, match="D_NOPE"):
        s.for_drive("D_NOPE")


def test_audience_split_follows_department():
    s = Settings(**BASE, departments=(CS,))
    assert s.for_drive("D_CS").audience_split_enabled is True


def test_json_round_trip():
    raw = json.dumps(
        {
            "cs": {
                "driveIds": ["D_CS"],
                "staffCorpus": "c-cs-staff",
                "studentCorpus": "c-cs-student",
                "sourceBucket": "b-cs-src",
                "hwpBucket": "b-cs-hwp",
                "syncFolderIds": ["F_SYNC"],
                "studentFolderIds": ["F_STU"],
            }
        }
    )
    (dept,) = _departments_from_json(raw)
    assert dept.code == "cs"
    assert dept.drive_ids == ("D_CS",)
    assert dept.student_folder_ids == ("F_STU",)


def test_json_accepts_comma_strings():
    (dept,) = _departments_from_json(
        json.dumps({"cs": {"driveIds": "D_A, D_B", "staffCorpus": "c",
                           "hwpBucket": "raw", "sourceBucket": "source",
                           "syncFolderIds": "folder"}})
    )
    assert dept.drive_ids == ("D_A", "D_B")


@pytest.mark.parametrize("raw", ["{깨진", "[]", "{}", "null", '"문자열"'])
def test_broken_json_is_rejected(raw: str):
    with pytest.raises(ValueError, match="DEPARTMENTS_JSON"):
        _departments_from_json(raw)


def test_partial_department_cannot_use_common_values():
    with pytest.raises(ValueError, match="buckets required"):
        Settings(**BASE, departments=(Department(code="bad", drive_ids=("D",), staff_corpus="c"),))


class _FakeStore:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get(self, file_id: str):
        drive = self._m.get(file_id)
        if drive is None:
            return None
        return type("S", (), {"drive_id": drive})()


def _sync():
    from services.sync import main as sync_main

    return sync_main


def test_settings_for_drive_passes_through_without_map():
    m = _sync()
    base = Settings(**BASE)
    assert m._settings_for_drive(base, "whatever") is base
    assert m._settings_for_drive(base, None) is base


def test_settings_for_drive_requires_drive_when_map_exists():
    m = _sync()
    base = Settings(**BASE, departments=(CS,))
    with pytest.raises(UnknownDriveError):
        m._settings_for_drive(base, None)


def test_settings_for_drive_tolerates_settings_without_the_field():
    m = _sync()
    stub = object()
    assert m._settings_for_drive(stub, "any") is stub


def test_split_by_drive_is_a_noop_without_map():
    m = _sync()
    base = Settings(**BASE)
    store = _FakeStore({})
    assert m._split_by_drive(store, ["gs://b/f1.md"], ["f1"], base) == [
        (base, ["gs://b/f1.md"], ["f1"])
    ]


def test_split_by_drive_groups_per_department():
    m = _sync()
    base = Settings(**BASE, departments=(CS, EE))
    store = _FakeStore({"f1": "D_CS", "f2": "D_EE"})
    groups = m._split_by_drive(
        store, ["gs://b/f1.md", "gs://b/f2.md"], ["f1", "f2"], base
    )
    by_corpus = {g[0].rag_corpus_name: (g[1], g[2]) for g in groups}
    assert by_corpus["c-cs-staff"] == (["gs://b/f1.md"], ["f1"])
    assert by_corpus["c-ee-staff"] == (["gs://b/f2.md"], ["f2"])


def test_split_by_drive_drops_documents_it_cannot_place():
    m = _sync()
    base = Settings(**BASE, departments=(CS,))
    store = _FakeStore({"f1": "D_CS", "f2": "D_UNKNOWN"})
    groups = m._split_by_drive(
        store, ["gs://b/f1.md", "gs://b/f2.md", "gs://b/f3.md"], ["f1", "f2", "f3"], base
    )
    assert len(groups) == 1
    settings, uris, ids = groups[0]
    assert settings.rag_corpus_name == "c-cs-staff"
    assert uris == ["gs://b/f1.md"]
    assert ids == ["f1"]


F1 = "1ZpC1Xnmkuk5S-1OGkHzrSqbtgBE4Fvas"
F2 = "162LbSmsoKKtagl_otv83PW6TAz5AAYxS"


def test_index_gcs_imports_into_each_department_corpus(monkeypatch):
    m = _sync()
    seen: list[tuple[str, list[str]]] = []

    class _Rag:
        def __init__(self, settings=None, **_kw):
            self._corpus = getattr(settings, "rag_corpus_name", "?")

        def delete_files_by_ids(self, _ids):
            return 0

        def import_from_gcs(self, uris):
            seen.append((self._corpus, list(uris)))
            return ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    class _Store(_FakeStore):
        def mark_indexed(self, _fid):
            pass

        def upsert(self, _state):
            pass

    store = _Store({F1: "D_CS", F2: "D_EE"})
    monkeypatch.setattr(m, "get_settings", lambda: Settings(**BASE, departments=(CS, EE)))
    monkeypatch.setattr(m, "DocStateStore", lambda: store)
    monkeypatch.setattr(m, "RagEngineClient", _Rag)
    monkeypatch.setattr(m, "_sync_student_corpus", lambda *_a, **_k: {"enabled": False})

    m.index_gcs(
        m.IndexGcsBody(
            gcsUris=[f"gs://b/{F1}.md", f"gs://b/{F2}.md"], fileIds=[F1, F2]
        )
    )

    by_corpus = dict(seen)
    assert by_corpus["c-cs-staff"] == [f"gs://b/{F1}.md"]
    assert by_corpus["c-ee-staff"] == [f"gs://b/{F2}.md"]


def test_reindex_pending_uses_each_department_corpus_and_bucket(monkeypatch):
    m = _sync()
    imported: list[tuple[str, list[str]]] = []
    buckets_queried: list[str] = []

    def _doc(fid, drive):
        return type(
            "D", (), {"file_id": fid, "drive_id": drive, "name": fid,
                      "mime_type": "application/pdf", "modified_time": "",
                      "source_uri": None}
        )()

    class _Store:
        def list_by_status(self, _status, limit=200, cursor_key=None):
            return [_doc(F1, "D_CS"), _doc(F2, "D_EE")]

        def get(self, _fid):
            return None

        def upsert(self, _s):
            pass

        def mark_indexed(self, _fid):
            pass

        def clear_dlq(self, _fid):
            pass

    class _Gcs:
        def __init__(self, settings=None):
            self._bucket = getattr(settings, "gcs_source_bucket", "?")

        def list_blob_names_for_file(self, bucket, file_id):
            buckets_queried.append(bucket)
            return [f"{file_id}.md"]

    class _Rag:
        def __init__(self, settings=None, **_kw):
            self._corpus = getattr(settings, "rag_corpus_name", "?")

        def delete_files_by_ids(self, _ids):
            return 0

        def import_from_gcs(self, uris):
            imported.append((self._corpus, list(uris)))
            return ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    monkeypatch.setattr(m, "get_settings", lambda: Settings(**BASE, departments=(CS, EE)))
    monkeypatch.setattr(m, "DocStateStore", _Store)
    monkeypatch.setattr(m, "GcsClient", _Gcs)
    monkeypatch.setattr(m, "RagEngineClient", _Rag)
    monkeypatch.setattr(m, "_sync_student_corpus", lambda *_a, **_k: {"enabled": False})

    result = m._reindex_pending_sync(m.ReindexPendingBody(background=False))

    by_corpus = dict(imported)
    assert by_corpus["c-cs-staff"] == [f"gs://b-cs-src/{F1}.md"]
    assert by_corpus["c-ee-staff"] == [f"gs://shared-src/{F2}.md"]
    assert set(buckets_queried) == {"b-cs-src", "shared-src"}
    assert result["totals"]["skippedNoUri"] == 0


def test_retry_failed_reingests_into_each_department_bucket(monkeypatch):
    m = _sync()
    seen: list[tuple[str, str]] = []

    def _doc(fid, drive):
        return type(
            "D", (), {"file_id": fid, "drive_id": drive, "name": fid,
                      "mime_type": "application/pdf", "modified_time": "",
                      "source_uri": None}
        )()

    class _Store:
        def list_by_status(self, _status, limit=100, cursor_key=None):
            return [_doc(F1, "D_CS"), _doc(F2, "D_EE")]

        def get_dlq_attempts(self, _fid):
            return 0

        def record_dlq_attempt(self, _fid):
            pass

        def clear_dlq(self, _fid):
            pass

    def _fake_ingest(body, *, store=None, settings=None, gcs=None, drive=None):
        seen.append((body.file_id, settings.gcs_source_bucket))
        return {"status": "SKIPPED"}

    monkeypatch.setattr(m, "get_settings", lambda: Settings(**BASE, departments=(CS, EE)))
    monkeypatch.setattr(m, "DocStateStore", _Store)
    monkeypatch.setattr(m, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(m, "DriveClient", lambda: object())
    monkeypatch.setattr(m, "_ingest_with", _fake_ingest)

    m.retry_failed(m.RetryFailedBody())

    by_file = dict(seen)
    assert by_file[F1] == "b-cs-src"
    assert by_file[F2] == "shared-src"


def test_out_of_scope_cleanup_uses_the_given_settings_corpus(monkeypatch):
    m = _sync()
    corpora: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None):
            self._corpus = corpus_name or getattr(settings, "rag_corpus_name", "?")

        def delete_by_file_id(self, _fid):
            corpora.append(self._corpus)
            return True

    class _Client:
        def list_blobs(self, bucket, prefix=None):
            buckets.append(bucket)
            return []

    class _Gcs:
        _client = _Client()

    buckets: list[str] = []
    monkeypatch.setattr(m, "RagEngineClient", _Rag)
    dept = Settings(**BASE, departments=(CS,)).for_drive("D_CS")
    m._cleanup_out_of_scope_file(_Gcs(), dept, F1)

    assert set(buckets) == {"b-cs-src", "b-cs-hwp"}, "버킷도 학과 것이어야 한다"
    assert "c-cs-staff" in corpora, "교직원 코퍼스가 학과 것이어야 한다"
    assert "c-cs-student" in corpora, "학생 코퍼스도 학과 것이어야 한다"
    assert "corpus-default" not in corpora


def test_delete_removes_from_the_department_corpus(monkeypatch):
    m = _sync()
    corpora: list[str] = []
    buckets: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None):
            self._corpus = corpus_name or getattr(settings, "rag_corpus_name", "?")

        def delete_by_file_id(self, _fid):
            corpora.append(self._corpus)
            return True

    class _Client:
        def list_blobs(self, bucket, prefix=None):
            buckets.append(bucket)
            return []

    class _Gcs:
        _client = _Client()

        def __init__(self, _s=None):
            pass

        def delete_for_file(self, bucket, _fid):
            buckets.append(bucket)
            return []

    class _Store:
        def get(self, _fid):
            return None

        def mark_deleted(self, _fid):
            pass

    monkeypatch.setattr(m, "get_settings", lambda: Settings(**BASE, departments=(CS, EE)))
    monkeypatch.setattr(m, "DocStateStore", _Store)
    monkeypatch.setattr(m, "GcsClient", _Gcs)
    monkeypatch.setattr(m, "RagEngineClient", _Rag)

    m.delete_file(m.DeleteBody(fileId=F1, driveId="D_CS"))

    assert set(corpora) == {"c-cs-staff", "c-cs-student"}
    assert set(buckets) == {"b-cs-src", "b-cs-hwp"}
