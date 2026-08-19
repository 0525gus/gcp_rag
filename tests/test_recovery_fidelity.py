"""복구 경로가 문서를 조용히 손상시키지 않는지 검증한다.

재시도·재색인·삭제는 '정상 동작 중'에 도는 경로라, 여기서 나는 오염은 로그도
지표도 남기지 않고 검색 결과로만 드러난다. 그래서 계약으로 못박는다.
"""

from __future__ import annotations

from typing import Any

import pytest

import services.sync.main as sync_main
from shared.gcs import GcsClient
from shared.rag_engine import ImportOutcome
from shared.models import DocState, DocStatus, SearchHit, SearchSource
from shared.search_postprocess import postprocess_hits


# ============================================================ #1 Drive 링크 보존
class _LinkStore:
    """upsert 된 DocState 를 그대로 붙잡아 둔다."""

    def __init__(self, doc: DocState) -> None:
        self._doc = doc
        self.saved: list[DocState] = []
        self.dlq: list[dict[str, Any]] = []

    # --- retry_failed 가 쓰는 것 ---
    def list_by_status(self, *_a: Any, **_k: Any) -> list[DocState]:
        return [self._doc]

    def get_dlq_attempts(self, _fid: str) -> int:
        return 0

    def record_dlq_attempt(self, _fid: str) -> None:
        pass

    def clear_dlq(self, _fid: str) -> None:
        pass

    # --- ingest 가 쓰는 것 ---
    def should_reparse(self, *_a: Any, **_k: Any) -> bool:
        return True

    def get(self, _fid: str) -> None:
        return None

    def should_skip_reindex(self, *_a: Any, **_k: Any) -> bool:
        return False

    def upsert(self, state: DocState) -> None:
        self.saved.append(state)

    def enqueue_dlq(self, file_id: str, reason: str, **fields: Any) -> None:
        self.dlq.append({"fileId": file_id, "reason": reason, **fields})


class _Gcs:
    def upload_source_md(self, _md: str, fid: str) -> str:
        return f"gs://norm/{fid}.md"


class _Drive:
    def download_file(self, _fid: str) -> bytes:
        return b"content"

    def resolve_path_context(self, _fid: str, name: str):
        from shared.path_context import build_path_context

        return build_path_context(["folder"], name)

    def is_in_sync_scope(self, *_a: Any, **_k: Any) -> bool:
        return True


class _Settings:
    sync_folder_id_list: list[str] = []
    student_folder_id_list: list[str] = []
    audience_split_enabled = False
    rag_corpus_name_student = ""
    max_gcs_bytes = 10**9
    gcs_source_bucket = "norm"
    gcs_hwp_original_bucket = "raw"


DRIVE_LINK = "https://drive.google.com/file/d/f1/view"


def _wire_retry(monkeypatch: pytest.MonkeyPatch, store: _LinkStore) -> None:
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: _Gcs())
    monkeypatch.setattr(sync_main, "DriveClient", lambda: _Drive())
    monkeypatch.setattr(
        sync_main, "index_gcs", lambda body: {"count": len(body.gcs_uris)}
    )


def test_retry_preserves_drive_link_instead_of_overwriting_with_gcs_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = DocState(
        file_id="f1",
        drive_id="d1",
        name="doc.txt",
        mime_type="text/plain",
        modified_time="2026-01-01T00:00:00Z",
        status=DocStatus.FAILED,
        source_uri=DRIVE_LINK,
    )
    store = _LinkStore(doc)
    _wire_retry(monkeypatch, store)

    sync_main.retry_failed(sync_main.RetryFailedBody())

    assert store.saved, "재시도가 문서를 저장하지 않았다"
    saved = store.saved[-1].source_uri
    assert saved == DRIVE_LINK, f"Drive 링크가 {saved!r} 로 덮였다"


def test_retry_does_not_resurrect_a_gcs_uri_as_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 이미 gs:// 로 오염된 문서를 링크인 척 되돌려 쓰면 안 된다.
    doc = DocState(
        file_id="f1",
        drive_id="d1",
        name="doc.txt",
        mime_type="text/plain",
        status=DocStatus.FAILED,
        source_uri="gs://norm/f1.md",
    )
    store = _LinkStore(doc)
    _wire_retry(monkeypatch, store)

    sync_main.retry_failed(sync_main.RetryFailedBody())

    assert store.saved[-1].source_uri.startswith("gs://")


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (DRIVE_LINK, DRIVE_LINK),
        ("http://example.test/x", "http://example.test/x"),
        ("gs://bucket/blob.md", None),
        ("", None),
        (None, None),
    ],
)
def test_drive_link_only_accepts_http(stored: str | None, expected: str | None) -> None:
    assert sync_main._drive_link(stored) == expected


def test_first_time_dlq_records_the_link_for_later_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 최초 ingest 에서 DLQ 로 갈 때 링크를 안 남기면 재시도가 복원할 근거가 없다.
    store = _LinkStore(DocState(file_id="f1", drive_id="d1"))
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: _Gcs())
    monkeypatch.setattr(sync_main, "DriveClient", lambda: _Drive())

    def _boom(*_a: Any, **_k: Any):
        raise RuntimeError("drive down")

    monkeypatch.setattr(sync_main, "_ingest_direct", _boom)

    sync_main.ingest(
        sync_main.IngestBody(
            fileId="f1",
            driveId="d1",
            name="doc.txt",
            mimeType="text/plain",
            webViewLink=DRIVE_LINK,
        )
    )

    assert store.dlq[-1]["sourceUri"] == DRIVE_LINK


# ============================================================ #5 삭제 누수
class _Blob:
    def __init__(self, name: str, sink: list[str]) -> None:
        self.name = name
        self._sink = sink

    def delete(self) -> None:
        self._sink.append(self.name)


class _FakeBucket:
    """실제 storage.Bucket 자리 — 이름만 들고 blob() 을 내준다."""

    def __init__(self, name: str, sink: list[str]) -> None:
        self.name = name
        self._sink = sink

    def blob(self, name: str) -> _Blob:
        return _Blob(name, self._sink)


class _StorageClient:
    def __init__(self, objects: dict[str, list[str]], sink: list[str]) -> None:
        self._objects = objects
        self._sink = sink

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(name, self._sink)

    def list_blobs(self, bucket: Any, prefix: str = "") -> list[_Blob]:
        key = bucket.name if isinstance(bucket, _FakeBucket) else bucket
        return [
            _Blob(n, self._sink)
            for n in self._objects.get(key, [])
            if n.startswith(prefix)
        ]


class _DeleteGcs:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self.deleted: list[str] = []
        self._client = _StorageClient(objects, self.deleted)

    # fileId 경계 규칙을 여기서 다시 적으면 본체와 어긋난다 — 본체를 그대로 빌린다.
    list_blob_names_for_file = GcsClient.list_blob_names_for_file
    delete_for_file = GcsClient.delete_for_file


class _DeleteStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def mark_deleted(self, file_id: str) -> None:
        self.deleted.append(file_id)


def _wire_delete(
    monkeypatch: pytest.MonkeyPatch, objects: dict[str, list[str]]
) -> tuple[_DeleteGcs, _DeleteStore]:
    gcs = _DeleteGcs(objects)
    store = _DeleteStore()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: gcs)
    monkeypatch.setattr(
        sync_main,
        "RagEngineClient",
        # settings(+corpus_name) 를 받는다 — 학생 코퍼스도 같은 클래스로 만든다.
        lambda *_a, **_k: type("R", (), {"delete_by_file_id": lambda _s, _f: True})(),
    )
    return gcs, store


def test_delete_removes_raw_original_and_unlisted_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gcs, store = _wire_delete(
        monkeypatch,
        {
            # 대문자·목록에 없는 확장자 — 구 구현은 전부 놓쳤다
            "norm": ["f1.DOCX", "f1.meta.md"],
            # raw 원본 — 구 구현은 아예 대상이 아니었다
            "raw": ["f1.hwp"],
        },
    )

    result = sync_main.delete_file(sync_main.DeleteBody(fileId="f1"))

    assert sorted(gcs.deleted) == ["f1.DOCX", "f1.hwp", "f1.meta.md"]
    assert result["hwpOriginalDeleted"] == 1
    assert result["sourceDeleted"] == 2
    assert store.deleted == ["f1"]


def test_delete_does_not_touch_a_file_whose_id_shares_a_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gcs, _ = _wire_delete(
        monkeypatch,
        {"norm": ["f1.md", "f10.md"], "raw": []},
    )

    sync_main.delete_file(sync_main.DeleteBody(fileId="f1"))

    assert gcs.deleted == ["f1.md"], "f1 삭제가 f10 까지 지웠다"


def test_delete_surfaces_gcs_failure_instead_of_swallowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gcs, store = _wire_delete(monkeypatch, {"norm": ["f1.md"], "raw": []})

    def _boom(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("permission denied")

    # 삭제는 GcsClient.delete_for_file 로 나간다 — 목록·삭제가 같은 fileId 경계
    # 규칙을 쓰도록 한 곳에 모아 둔 지점이다.
    monkeypatch.setattr(gcs, "delete_for_file", _boom)

    with pytest.raises(sync_main.HTTPException) as exc:
        sync_main.delete_file(sync_main.DeleteBody(fileId="f1"))

    assert exc.value.status_code == 500
    # 코퍼스는 이미 정리됐으므로 상태는 DELETED 로 남긴다
    assert store.deleted == ["f1"]


# ============================================================ #3 사이드카 가림
def _hit(text: str, name: str, score: float = 0.5) -> SearchHit:
    return SearchHit(
        text=text,
        score=score,
        source=SearchSource(
            file_id="",
            name=name,
            source_uri=f"gs://norm/{name}",
        ),
    )


SIDECAR_TEXT = "이 파일은 자료묶음 `2026 교육` 소속입니다. 동일 경로의 관련 PDF와 함께 참조하세요."


def test_sidecar_does_not_shadow_the_real_content_of_the_same_file() -> None:
    # sidecar 가 상위에 걸려도 본문이 있으면 본문이 나와야 한다.
    hits = [
        _hit(SIDECAR_TEXT, "f1.meta.md", score=0.1),
        _hit("교원업적평가 시행 계획은 다음과 같다...", "f1.pdf", score=0.4),
    ]

    out = postprocess_hits(hits, top_k=5)

    assert len(out) == 1
    assert "교원업적평가" in out[0].text
    assert SIDECAR_TEXT not in out[0].text


def test_sidecar_survives_when_it_is_the_only_hit_for_that_file() -> None:
    hits = [
        _hit(SIDECAR_TEXT, "f1.meta.md"),
        _hit("다른 문서 본문", "f2.pdf"),
    ]

    out = postprocess_hits(hits, top_k=5)

    ids = {h.source.file_id for h in out}
    assert ids == {"f1", "f2"}, "본문이 없는 파일의 sidecar 까지 버리면 문서를 통째로 잃는다"


def test_sidecar_removal_frees_a_slot_for_another_document() -> None:
    hits = [
        _hit(SIDECAR_TEXT, "f1.meta.md"),
        _hit("f1 본문", "f1.pdf"),
        _hit("f2 본문", "f2.pdf"),
    ]

    out = postprocess_hits(hits, top_k=2)

    assert [h.source.file_id for h in out] == ["f1", "f2"]
    assert out[0].text == "f1 본문"


# ============================================================ #2 재색인 스테일
class _StaleStore:
    def __init__(self, docs: list[DocState]) -> None:
        self._docs = docs
        self.indexed: list[str] = []

    def list_by_status(self, status: DocStatus, **_k: Any) -> list[DocState]:
        return [d for d in self._docs if d.status == status]

    def get(self, fid: str) -> DocState | None:
        return next((d for d in self._docs if d.file_id == fid), None)

    def upsert(self, state: DocState) -> None:
        if state.status == DocStatus.INDEXED:
            self.indexed.append(state.file_id)

    def clear_dlq(self, _fid: str) -> None:
        pass

    def enqueue_dlq(self, *_a: Any, **_k: Any) -> None:
        pass


def test_reindex_deletes_existing_chunks_before_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """삭제를 건너뛰면 Vertex 가 skip → 구버전 청크가 남은 채 INDEXED 가 된다."""
    order: list[str] = []
    docs = [DocState(file_id="f1", drive_id="d", status=DocStatus.PARSED)]
    store = _StaleStore(docs)

    class _Rag:
        def delete_files_by_ids(self, file_ids: list[str]) -> int:
            order.append(f"delete:{sorted(file_ids)}")
            return len(file_ids)

        def import_from_gcs(self, uris: list[str]) -> ImportOutcome:
            order.append(f"import:{len(uris)}")
            return ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(
        sync_main,
        "_source_uris_for_file",
        lambda _s, fid, _c=None: [f"gs://norm/{fid}.md"],
    )

    result = sync_main._reindex_pending_sync(sync_main.ReindexPendingBody())

    assert order == ["delete:['f1']", "import:1"], "import 전에 기존 청크를 지워야 한다"
    assert store.indexed == ["f1"]
    assert result["ok"] is True


def test_reindex_scans_the_corpus_only_once_for_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """삭제는 배치마다(지우는 집합 = 넣는 집합), 코퍼스 순회는 run 당 1회.

    미리 전체를 지우면 뒤 배치가 실패했을 때 그 문서가 청크 없이 남는다. 그렇다고
    배치마다 코퍼스를 새로 순회하면 배치 수만큼 전수 순회가 붙는다. 클라이언트를
    공유해 첫 순회 스냅샷을 재사용하는 것으로 둘 다 만족시킨다.
    """
    docs = [
        DocState(file_id=f"f{i}", drive_id="d", status=DocStatus.PARSED)
        for i in range(6)
    ]
    store = _StaleStore(docs)
    deletes: list[list[str]] = []
    scans: list[int] = []

    class _Rag:
        """실제 RagEngineClient 처럼 첫 삭제에서만 코퍼스를 순회한다."""

        def __init__(self) -> None:
            self._scanned = False

        def delete_files_by_ids(self, file_ids: list[str]) -> int:
            if not self._scanned:
                self._scanned = True
                scans.append(1)
            deletes.append(sorted(file_ids))
            return len(file_ids)

        def import_from_gcs(self, uris: list[str]) -> ImportOutcome:
            return ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(
        sync_main,
        "_source_uris_for_file",
        lambda _s, fid, _c=None: [f"gs://norm/{fid}.md"],
    )

    sync_main._reindex_pending_sync(sync_main.ReindexPendingBody(indexBatchSize=2))

    assert len(scans) == 1, f"코퍼스를 {len(scans)}회 순회했다 — 1회여야 한다"
    # 삭제는 배치 단위로, 그 배치가 import 할 파일만 대상으로.
    assert deletes == [["f0", "f1"], ["f2", "f3"], ["f4", "f5"]]
