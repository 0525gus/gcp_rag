"""학과 라우팅 — driveId → 코퍼스·버킷·폴더. GCP 호출 없음.

학과 = 공유 드라이브 전제. `Settings.for_drive()` 를 핸들러 진입점에서 한 번
갈아끼우면 그 아래 호출(RagEngineClient·GcsClient·폴더 스코프)이 전부 학과
값을 쓴다. 그래서 이 파일이 지키는 것은 사실상 두 가지다.

  1. **맵이 비면 지금까지와 똑같이 동작한다** — 기존 배포를 안 깨는 장치
  2. **모르는 드라이브가 기본 코퍼스로 조용히 떨어지지 않는다** — 그건 남의
     학과 자료가 섞이는 사고이고, 되돌리려면 코퍼스에서 파일을 골라 지워야 한다
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config import Department, Settings, UnknownDriveError, _departments_from_json
from shared.rag_engine import ImportOutcome

BASE = {
    "gcp_project_id": "p",
    "gcs_hwp_original_bucket": "shared-hwp",
    "gcs_source_bucket": "shared-src",
    "rag_corpus_name": "corpus-default",
    "rag_corpus_name_student": "corpus-default-student",
    "sync_folder_ids": "SHARED_FOLDER",
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
# 일부만 적은 학과. 기존 학과를 옮기지 않고 새 학과만 자기 버킷을 갖는 이관을 흉내낸다.
EE = Department(code="ee", drive_ids=("D_EE",), staff_corpus="c-ee-staff")


# --- 하위 호환 -------------------------------------------------------------

def test_empty_map_keeps_current_behaviour():
    """맵이 비면 for_drive 가 자기 자신을 준다 — 기존 배포가 안 바뀐다."""
    s = Settings(**BASE)
    assert s.for_drive("anything") is s
    assert s.department_for_drive("anything") is None


def test_omitted_fields_inherit_shared_values():
    s = Settings(**BASE, departments=(CS, EE))
    ee = s.for_drive("D_EE")
    assert ee.rag_corpus_name == "c-ee-staff"
    # 안 적은 것은 공용값 그대로
    assert ee.rag_corpus_name_student == "corpus-default-student"
    assert ee.gcs_source_bucket == "shared-src"
    assert ee.sync_folder_ids == "SHARED_FOLDER"


# --- 라우팅 ----------------------------------------------------------------

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
    # 원본은 그대로 — replace 사본이라 서로 오염되지 않는다
    assert s.rag_corpus_name == "corpus-default"


def test_unknown_drive_raises_instead_of_defaulting():
    """조용히 기본 코퍼스로 떨어지면 남의 학과 자료가 섞인다."""
    s = Settings(**BASE, departments=(CS,))
    with pytest.raises(UnknownDriveError, match="D_NOPE"):
        s.for_drive("D_NOPE")


def test_audience_split_follows_department():
    s = Settings(**BASE, departments=(CS,))
    assert s.for_drive("D_CS").audience_split_enabled is True


# --- DEPARTMENTS_JSON 파싱 -------------------------------------------------

def test_json_round_trip():
    raw = json.dumps(
        {
            "cs": {
                "driveIds": ["D_CS"],
                "staffCorpus": "c-cs-staff",
                "studentCorpus": "c-cs-student",
                "sourceBucket": "b-cs-src",
                "studentFolderIds": ["F_STU"],
            }
        }
    )
    (dept,) = _departments_from_json(raw)
    assert dept.code == "cs"
    assert dept.drive_ids == ("D_CS",)
    assert dept.student_folder_ids == ("F_STU",)


def test_json_accepts_comma_strings():
    """저장소 관례가 쉼표 구분이라 리스트/문자열 둘 다 받는다."""
    (dept,) = _departments_from_json(
        json.dumps({"cs": {"driveIds": "D_A, D_B", "staffCorpus": "c"}})
    )
    assert dept.drive_ids == ("D_A", "D_B")


@pytest.mark.parametrize("raw", ["", "   ", "{깨진", "[]", "null", '"문자열"'])
def test_broken_json_falls_back_to_single_department(raw: str):
    """설정 오타 하나로 sync 가 기동조차 못 하면 안 된다 — 비우고 경고만."""
    assert _departments_from_json(raw) == ()


def test_broken_json_is_logged(caplog):
    """조용히 비우면 '학과 설정이 왜 안 먹지' 를 추적할 수 없다."""
    with caplog.at_level("ERROR"):
        _departments_from_json("{깨진")
    assert any("DEPARTMENTS_JSON" in r.message for r in caplog.records)


# --- sync 배선 헬퍼 ---------------------------------------------------------

class _FakeStore:
    """doc_state 대역. fileId -> driveId 만 있으면 된다."""

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
    """맵이 없으면 driveId 가 없어도 그냥 통과한다 — 기존 동작."""
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
    """테스트·스크립트가 넘기는 가벼운 설정 대역에는 departments 가 없다."""
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
    """어느 학과인지 모르는 문서를 기본 코퍼스에 넣으면 남의 학과에 섞인다.

    안 넣으면 다음 주기가 다시 집는다 — 그쪽이 싼 실패다.
    """
    m = _sync()
    base = Settings(**BASE, departments=(CS,))
    store = _FakeStore({"f1": "D_CS", "f2": "D_UNKNOWN"})  # f3 는 doc_state 자체가 없음
    groups = m._split_by_drive(
        store, ["gs://b/f1.md", "gs://b/f2.md", "gs://b/f3.md"], ["f1", "f2", "f3"], base
    )
    assert len(groups) == 1
    settings, uris, ids = groups[0]
    assert settings.rag_corpus_name == "c-cs-staff"
    assert uris == ["gs://b/f1.md"]
    assert ids == ["f1"]


# 실제 Drive fileId 모양이어야 _clean_file_ids 를 통과한다.
F1 = "1ZpC1Xnmkuk5S-1OGkHzrSqbtgBE4Fvas"
F2 = "162LbSmsoKKtagl_otv83PW6TAz5AAYxS"


def test_index_gcs_imports_into_each_department_corpus(monkeypatch):
    """driveId 가 없는 유일한 엔드포인트. 학과별로 갈라 넣는지 본다.

    안 가르면 전부 전역 기본 코퍼스로 들어가 학과가 둘 이상인 순간 섞인다.
    """
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
        """doc_state 대역 — 이 테스트가 보는 것은 '어느 코퍼스로 갔나' 뿐이다."""

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
    """복구 경로도 학과별로 갈라야 한다.

    복구 대상은 status 로만 뽑혀(list_by_status 에 드라이브 필터가 없다) 전 학과가
    한 배치에 섞인다. 갈라지 않으면 두 가지가 동시에 깨진다.
      - 코퍼스: 전부 전역 기본 하나로 들어간다 (섞임)
      - 버킷:   전역 버킷에서 URI 를 못 찾아 전부 DLQ 로 간다 (유실)
    """
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
    assert by_corpus["c-ee-staff"] == [f"gs://shared-src/{F2}.md"]  # ee 는 공용 상속
    # 버킷도 학과 것을 뒤져야 한다 — 아니면 URI 를 못 찾아 DLQ 로 간다
    assert set(buckets_queried) == {"b-cs-src", "shared-src"}
    assert result["totals"]["skippedNoUri"] == 0


def test_retry_failed_reingests_into_each_department_bucket(monkeypatch):
    """DLQ 회수도 학과별이어야 한다.

    코퍼스 쪽은 flush() 가 index_gcs() 를 부르므로 이미 갈라진다. 남는 위험은
    **쓰기 버킷**이다 — 전역 settings 로 _ingest_with 를 부르면 ee 문서의 원본과
    변환 산출물이 cs 버킷에 저장된다. 그 뒤 재색인은 ee 버킷을 뒤지므로 못 찾고,
    문서는 다시 DLQ 로 돌아온다(회수 장치가 무한 루프가 된다).
    """
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
        return {"status": "SKIPPED"}   # 색인 경로는 이 테스트의 관심사가 아니다

    monkeypatch.setattr(m, "get_settings", lambda: Settings(**BASE, departments=(CS, EE)))
    monkeypatch.setattr(m, "DocStateStore", _Store)
    monkeypatch.setattr(m, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(m, "DriveClient", lambda: object())
    monkeypatch.setattr(m, "_ingest_with", _fake_ingest)

    m.retry_failed(m.RetryFailedBody())

    by_file = dict(seen)
    assert by_file[F1] == "b-cs-src"      # cs 는 자기 버킷
    assert by_file[F2] == "shared-src"    # ee 는 공용 상속


def test_out_of_scope_cleanup_uses_the_given_settings_corpus(monkeypatch):
    """범위 밖 정리가 전역 코퍼스를 지우면 실제 코퍼스에는 문서가 남는다.

    같은 함수 안에서 학생 코퍼스는 settings 를 쓰는데 교직원 코퍼스만 인자 없이
    만들고 있었다 — 학과를 갈라도 교직원 쪽은 전역을 지운다.
    """
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
    """Drive 에서 지운 문서를 엉뚱한 코퍼스에서 지우면 계속 검색된다."""
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
