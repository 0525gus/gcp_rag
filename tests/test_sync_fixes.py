"""sync 파이프라인 회귀 수정 검증 (#1~#5).

#1 URI vs 파일 카운팅 (reconcile index_ok / uris)
#2 reconcile 이중 집계 (dlq/split ⊂ failed)
#3 색인 누락 복구 (should_skip_reindex)
#4 status 대소문자 (out-of-scope → SKIPPED)
#5 배치 삭제 코퍼스 1회 순회 (delete_files_by_ids)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config import Settings  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.models import DocState, DocStatus  # noqa: E402
from shared import rag_engine  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402
import services.sync.main as sync_main  # noqa: E402
from services.sync.main import ReconcileBody, reconcile  # noqa: E402


# ---------------------------------------------------------------- #3
class _FakeStore(DocStateStore):
    def __init__(self, doc: DocState | None) -> None:  # firestore client 우회
        self._doc = doc

    def get(self, file_id: str) -> DocState | None:  # type: ignore[override]
        return self._doc


def test_should_skip_reindex_indexed_same_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is True


def test_should_skip_reindex_parsed_not_indexed() -> None:
    # PARSED(색인만 실패) + 같은 해시 → 스킵 금지 (색인 복구 필요)
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.PARSED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is False


def test_should_skip_reindex_diff_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h2") is False


def test_should_skip_reindex_missing() -> None:
    assert _FakeStore(None).should_skip_reindex("f1", "h1") is False


# ---------------------------------------------------------------- #5
class _FakeRagFile:
    def __init__(self, display: str, name: str) -> None:
        self.display_name = display
        self.name = name


def _bare_client(corpus: str) -> RagEngineClient:
    """__init__(vertexai.init) 우회. 인덱스 캐시가 읽는 필드만 손으로 채운다.

    캐시는 corpus_name 을 키로 쓰는 모듈 전역이라, 테스트마다 다른 이름을 주고
    이전 잔재를 지워야 옆 테스트의 인덱스를 물려받지 않는다.
    """
    client = object.__new__(RagEngineClient)
    client.corpus_name = corpus
    client.settings = get_settings_for_test()
    rag_engine._CORPUS_INDEX_CACHE.pop(corpus, None)
    rag_engine._CORPUS_DIRTY_IDS.pop(corpus, None)
    return client


def get_settings_for_test() -> Settings:
    return Settings(
        gcp_project_id="p",
        rag_delete_concurrency=1,
        rag_delete_pacing_seconds=0.0,
    )


def test_delete_files_by_ids_single_list(monkeypatch) -> None:
    client = _bare_client("corpus-single-list")
    calls = {"list": 0, "deleted": []}

    def fake_list() -> list[_FakeRagFile]:
        calls["list"] += 1
        return [
            _FakeRagFile("f1.md", "rn1"),
            _FakeRagFile("f1.meta.md", "rn2"),
            _FakeRagFile("f2.pdf", "rn3"),
            _FakeRagFile("other.md", "rn4"),
        ]

    client.list_files = fake_list  # type: ignore[method-assign]
    monkeypatch.setattr(
        rag_engine.rag, "delete_file", lambda name: calls["deleted"].append(name)
    )

    n = client.delete_files_by_ids(["f1", "f2"])
    assert calls["list"] == 1  # 핵심: 배치 전체에 코퍼스 1회 순회
    assert n == 3
    assert set(calls["deleted"]) == {"rn1", "rn2", "rn3"}


def test_delete_files_by_ids_empty(monkeypatch) -> None:
    client = _bare_client("corpus-empty")

    def boom() -> list:
        raise AssertionError("list_files should not be called for empty ids")

    client.list_files = boom  # type: ignore[method-assign]
    assert client.delete_files_by_ids([]) == 0


# ---------------------------------------------------------------- #1 / #2
def test_reconcile_no_double_count_dlq() -> None:
    # 5 listed = 2 gcs + 1 unchanged + 1 skipped + 1 failed(=dlq). dlq는 failed 하위.
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=1, dlq=1, splitQueued=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 0
    assert r["ok"] is True


def test_reconcile_index_ok_uses_uris() -> None:
    # 파일 1개 = 2 URI → indexed=2 > gcs=1 이지만 uris=2라 정합해야 함
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, uris=2, indexed=2,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True
    assert r["ok"] is True


def test_reconcile_uris_fallback_to_gcs() -> None:
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, indexed=1,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True


def test_reconcile_detects_real_gap() -> None:
    # listed=5, 실제 4만 계정 → 진짜 누락 1건은 여전히 잡아야 함
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 1
    assert r["ok"] is False


# ---------------------------------------------------------------- #4
def test_ingest_out_of_scope_returns_uppercase_excluded(monkeypatch) -> None:
    from services.sync.main import IngestBody, ingest

    class FakeSettings:
        sync_folder_id_list = ["folderX"]

    class FakeStore:
        def upsert(self, *a, **k) -> None:
            pass

    class FakeDrive:
        def is_in_sync_scope(self, file_id, folder_ids) -> bool:
            return False

    monkeypatch.setattr(sync_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: FakeStore())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: object())
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: FakeDrive())

    res = ingest(IngestBody(fileId="f1", driveId="d", mimeType="application/pdf"))
    # 원래 이 테스트가 고정하려던 것은 '대문자 enum 값'이었다. 거기에 더해
    # 이제 폴더 밖은 SKIPPED 가 아니라 EXCLUDED 로 갈린다 — 집계에서 빠져야
    # skipped 가 '대상인데 처리 못 함'만 가리키기 때문이다.
    assert res["status"] == DocStatus.EXCLUDED.value == "EXCLUDED"
    assert res["reason"] == "out_of_folder_scope"


# ------------------------------------------------- GCS 삭제 범위 (prefix 훑기)
class _FakeBlob:
    def __init__(self, name: str, sink: list[str]) -> None:
        self.name = name
        self._sink = sink

    def delete(self) -> None:
        self._sink.append(self.name)


class _FakeGcsBackend:
    """list_blobs / bucket().blob() 만 흉내내는 최소 스텁."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.deleted: list[str] = []

    def bucket(self, _name: str) -> object:
        backend = self

        class _B:
            @staticmethod
            def blob(n: str) -> _FakeBlob:
                return _FakeBlob(n, backend.deleted)

        return _B()

    def list_blobs(self, _bucket, prefix: str = "") -> list[_FakeBlob]:
        return [_FakeBlob(n, self.deleted) for n in self.names if n.startswith(prefix)]


def _fake_gcs(names: list[str]):
    from shared.gcs import GcsClient

    client = object.__new__(GcsClient)
    backend = _FakeGcsBackend(names)
    client._client = backend  # noqa: SLF001
    return client, backend


def test_delete_for_file_catches_suffixes_a_hardcoded_list_missed() -> None:
    """손으로 적은 확장자 목록이 놓쳤던 것들 — 분할 PDF 조각과 .rtf."""
    client, backend = _fake_gcs(
        [
            "normalized/abc123.part1.pdf",
            "normalized/abc123.part2.pdf",
            "normalized/abc123.meta.md",
            "normalized/abc123.rtf",
        ]
    )
    removed = client.delete_for_file("b", "normalized", "abc123")
    assert len(removed) == 4
    assert sorted(backend.deleted) == sorted(removed)


def test_delete_for_file_respects_file_id_boundary() -> None:
    """prefix 만으로 걸면 fileId 가 남의 fileId 접두사일 때 남의 파일을 지운다."""
    client, backend = _fake_gcs(
        [
            "normalized/abc123.md",
            "normalized/abc123456.md",  # 다른 문서 — 건드리면 안 된다
            "normalized/abc123",  # 확장자 없는 정확 일치는 대상
        ]
    )
    removed = client.delete_for_file("b", "normalized", "abc123")
    assert sorted(removed) == ["normalized/abc123", "normalized/abc123.md"]
    assert "normalized/abc123456.md" not in backend.deleted


def test_delete_file_also_clears_the_raw_original(monkeypatch) -> None:
    """Drive 에서 지운 문서의 원본이 raw 에 남아 있었다(실측 52건)."""
    from services.sync.main import DeleteBody, delete_file

    class FakeSettings:
        gcs_normalized_bucket = "norm"
        gcs_raw_bucket = "raw-b"
        # 학생/교직원 분리가 꺼진 기본 배포 형상
        audience_split_enabled = False
        rag_corpus_name_student = ""

    client, backend = _fake_gcs(
        ["normalized/abc123.md", "raw/abc123.hwp"]
    )
    monkeypatch.setattr(sync_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: client)
    monkeypatch.setattr(
        sync_main, "RagEngineClient", lambda *a, **k: type(
            "_R", (), {"delete_by_file_id": staticmethod(lambda _f: True)}
        )()
    )
    monkeypatch.setattr(
        sync_main, "DocStateStore", lambda *a, **k: type(
            "_S", (), {"mark_deleted": staticmethod(lambda _f: None)}
        )()
    )

    res = delete_file(DeleteBody(fileId="abc123"))
    assert res["gcsDeleted"] == 2
    assert "raw/abc123.hwp" in backend.deleted


# ------------------------------------------------------------- 빈 fileId
def test_ingest_rejects_blank_file_id() -> None:
    """빈 fileId 는 400 이어야 한다 — Firestore 경로가 `doc_state/` 가 되어 500 났다."""
    import pytest
    from fastapi import HTTPException

    from services.sync.main import IngestBody, ingest

    with pytest.raises(HTTPException) as exc:
        ingest(IngestBody(fileId="", driveId="d", mimeType="application/pdf"))
    assert exc.value.status_code == 400


def test_list_changes_drops_entries_without_file_id(monkeypatch) -> None:
    """fileId 없는 change(공유 드라이브 자체 변경)는 목록에서 빠져야 한다.

    흘려보내면 빈 id 로 Drive 를 조회해 400 이 나고, 이어지는 upsert 가
    Firestore 400 으로 죽어 /sync/ingest 가 500 을 낸다(7/23·24·29 실측).
    """
    from shared.drive import DriveClient

    client = object.__new__(DriveClient)

    class _Changes:
        @staticmethod
        def list(**_kwargs):
            class _Req:
                @staticmethod
                def execute(**_k):
                    return {
                        "newStartPageToken": "t2",
                        "changes": [
                            {"fileId": "", "file": {}},  # 드라이브 자체 변경
                            {
                                "fileId": "realid1234",
                                "file": {"id": "realid1234", "name": "a.pdf"},
                            },
                        ],
                    }

            return _Req()

    client._service = type("_S", (), {"changes": staticmethod(lambda: _Changes())})()

    changes, token = client.list_changes("drive1", "t1")
    assert [c.file_id for c in changes] == ["realid1234"]
    assert token == "t2"


# ---------------------------------------------------------------- workflow YAML
def test_workflow_yaml_parses_and_uses_uris_gate() -> None:
    import yaml

    p = ROOT / "workflows" / "daily_sync.yaml"
    txt = p.read_text(encoding="utf-8")
    data = yaml.safe_load(txt)
    assert "main" in data
    assert "drive_indexed == drive_uris" in txt
    assert "drive_indexed == drive_gcs" not in txt
    assert "uris: ${drive_uris}" in txt


def test_workflow_runs_recovery_after_all_drives() -> None:
    """자동 회수 단계가 드라이브 루프 뒤에 남아 있어야 DLQ가 고착되지 않는다."""
    import yaml

    data = yaml.safe_load((ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8"))
    names = [next(iter(step)) for step in data["main"]["steps"]]

    assert names.index("for_each_drive") < names.index("recover_pending_index")
    assert names.index("recover_pending_index") < names.index("recover_failed")
    assert names.index("recover_failed") < names.index("return_summary")


def test_workflow_recovery_calls_both_endpoints() -> None:
    txt = (ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8")
    assert "/sync/reindex-pending" in txt
    assert "/sync/retry-failed" in txt


# ---------------------------------------------------------------- import 재시도
def test_import_retries_on_corpus_busy(monkeypatch) -> None:
    """코퍼스 동시 작업(FailedPrecondition)은 기다리면 풀린다 — 재시도해야 한다.

    2026-07-29 재색인에서 48건이 이걸로 즉시 실패했다. 당시엔
    ResourceExhausted 만 재시도 대상이었다.
    """
    from google.api_core import exceptions as gcp_exceptions

    calls: list[int] = []

    def _flaky(corpus, uris, transformation_config=None):  # noqa: ANN001
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(
                "Failed in importing the RagFiles due to: ",
            ) from gcp_exceptions.FailedPrecondition("other operations running")
        return type("R", (), {"imported_rag_files_count": len(uris)})()

    monkeypatch.setattr(rag_engine.rag, "import_files", _flaky)
    monkeypatch.setattr(rag_engine.time, "sleep", lambda *_: None)

    client = RagEngineClient.__new__(RagEngineClient)
    client.corpus_name = "corpora/x"  # type: ignore[attr-defined]
    out = client._import_batch(["gs://b/a.md"], None, max_retries=5)

    assert out.uris == ["gs://b/a.md"]
    assert out.ok
    assert len(calls) == 3, "재시도 없이 즉시 실패하면 안 된다"


# ------------------------------------------------- import 결과 검증 (조용한 실패)
# `rag.import_files` 는 **호출 자체가 실패할 때만** 예외를 던진다. 파일 단위
# 거부는 응답의 failed/skipped 카운트로 온다. 예전에는 그 카운트를 로그로만
# 흘리고 보낸 목록을 그대로 성공으로 돌려줘서, 거부된 파일이 INDEXED 로 찍혔다
# (실측: xlsx 27건이 상시 거부 중이었는데 집계는 전부 성공).
def _fake_response(imported: int, failed: int = 0, skipped: int = 0):
    return type(
        "R",
        (),
        {
            "imported_rag_files_count": imported,
            "failed_rag_files_count": failed,
            "skipped_rag_files_count": skipped,
        },
    )()


def _client() -> RagEngineClient:
    client = RagEngineClient.__new__(RagEngineClient)
    client.corpus_name = "corpora/x"  # type: ignore[attr-defined]
    return client


def test_import_reports_partial_failure(monkeypatch) -> None:
    uris = [f"gs://b/f{i}.md" for i in range(3)]
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=2, failed=1),
    )
    out = _client()._import_batch(uris, None, max_retries=1)

    assert out.imported == 2
    assert out.failed == 1
    assert not out.ok, "거부된 파일이 있는데 성공으로 보고하면 안 된다"


def test_import_counts_skipped_as_ok(monkeypatch) -> None:
    """skipped 를 실패로 세면 pre-delete 생략 경로가 무한 루프에 빠진다.

    `reindex-pending` 비-force 는 속도 때문에 pre-delete 를 생략한다. 이미
    코퍼스에 있는 문서를 다시 import 하면 skipped 로 잡힐 수 있는데, 이걸
    실패로 보면 그 문서는 INDEXED 가 되지 못하고 매일 다시 import 된다.
    """
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=0, skipped=1),
    )
    assert _client()._import_batch(["gs://b/a.md"], None, max_retries=1).ok


def test_import_failure_still_not_ok_despite_skipped(monkeypatch) -> None:
    # skipped 를 관대하게 세더라도 failed 는 그대로 실패다
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=1, failed=1, skipped=1),
    )
    out = _client()._import_batch(
        ["gs://b/a.md", "gs://b/b.md", "gs://b/c.md"], None, max_retries=1
    )
    assert not out.ok


def test_import_outcome_survives_missing_counts(monkeypatch) -> None:
    # SDK 가 카운트를 안 주면 비관하지 않는다 — 여기서 실패로 처리하면
    # 필드명이 바뀌는 날 파이프라인이 통째로 멈춘다.
    monkeypatch.setattr(
        rag_engine.rag, "import_files", lambda *a, **k: type("R", (), {})()
    )
    out = _client()._import_batch(["gs://b/a.md"], None, max_retries=1)
    assert out.imported == 1
    assert out.ok


def test_import_aggregates_across_subbatches(monkeypatch) -> None:
    # URI 25개 상한으로 쪼개지는데, 뒤 배치의 실패가 앞 배치에 묻히면 안 된다
    seen: list[int] = []

    def _import(corpus, uris, transformation_config=None):
        seen.append(len(uris))
        # 두 번째 서브배치에서만 실패
        return _fake_response(imported=len(uris) - (1 if len(seen) == 2 else 0),
                              failed=1 if len(seen) == 2 else 0)

    monkeypatch.setattr(rag_engine.rag, "import_files", _import)
    monkeypatch.setattr(rag_engine.time, "sleep", lambda *_: None)

    client = _client()
    client.settings = Settings(gcp_project_id="p", rag_corpus_name="corpora/x")
    out = client.import_from_gcs([f"gs://b/f{i}.md" for i in range(30)])

    assert seen == [25, 5], "25개 상한으로 쪼개져야 한다"
    assert out.failed == 1
    assert not out.ok
    assert len(out.uris) == 30


def test_index_gcs_keeps_parsed_on_partial_failure(monkeypatch) -> None:
    """부분 실패 시 INDEXED 로 올리면 reindex-pending 이 영영 못 집는다."""
    marked: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None) -> None:
            pass

        def delete_files_by_ids(self, ids):
            return 0

        def import_from_gcs(self, uris):
            return rag_engine.ImportOutcome(
                uris=list(uris), imported=len(uris) - 1, failed=1, skipped=0
            )

    class _Store:
        def mark_indexed(self, fid):
            marked.append(fid)

    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: _Store())
    monkeypatch.setattr(
        sync_main, "get_settings",
        lambda: Settings(gcp_project_id="p", rag_corpus_name="corpora/x"),
    )

    res = sync_main.index_gcs(
        sync_main.IndexGcsBody(
            gcsUris=["gs://b/aaaaaaaaaaaa.md", "gs://b/bbbbbbbbbbbb.md"],
            fileIds=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        )
    )

    assert marked == [], "부분 실패인데 INDEXED 로 찍으면 안 된다"
    assert res["ok"] is False
    assert res["status"] == "PARTIAL"
    assert res["count"] == 1, "count 는 보낸 수가 아니라 실제 색인 수여야 한다"


def test_index_gcs_marks_indexed_on_full_success(monkeypatch) -> None:
    marked: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None) -> None:
            pass

        def delete_files_by_ids(self, ids):
            return 0

        def import_from_gcs(self, uris):
            return rag_engine.ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    class _Store:
        def mark_indexed(self, fid):
            marked.append(fid)

    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: _Store())
    monkeypatch.setattr(
        sync_main, "get_settings",
        lambda: Settings(gcp_project_id="p", rag_corpus_name="corpora/x"),
    )

    res = sync_main.index_gcs(
        sync_main.IndexGcsBody(
            gcsUris=["gs://b/aaaaaaaaaaaa.md"], fileIds=["aaaaaaaaaaaa"]
        )
    )

    assert marked == ["aaaaaaaaaaaa"]
    assert res["ok"] is True
    assert res["status"] == "INDEXED"
