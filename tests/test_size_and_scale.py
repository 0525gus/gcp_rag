"""크기 한도·대량 경로 계약.

RAG Engine 은 한도를 넘은 파일을 잘라 주지 않고 import 를 거부한다. 게이트가
느슨하면 문서가 통과한 뒤 index 단계에서 배치 전체를 실패로 돌리고, 그 문서는
PARSED 에 머물며 매일 재시도만 반복한다 — 로그 말고는 드러나지 않는다.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import pytest

import services.sync.main as sync_main
from shared.models import DocState, DocStatus

# services.mcp_server.main 은 임포트 시점에 get_settings() 를 부른다.
# 아래 검색 테스트가 그 모듈을 들여오므로 수집 시점에 필수 환경을 세워 둔다.
for _key, _val in (
    ("GCP_PROJECT_ID", "test-project"),
    ("GCS_RAW_BUCKET", "raw"),
    ("GCS_NORMALIZED_BUCKET", "norm"),
    ("RAG_CORPUS_NAME", "projects/p/locations/l/ragCorpora/c"),
):
    os.environ.setdefault(_key, _val)

MB = 1024 * 1024


class _Settings:
    sync_folder_id_list: list[str] = []
    max_gcs_bytes = 50 * MB
    gcs_normalized_bucket = "norm"
    gcs_raw_bucket = "raw"
    raw_upload_concurrency = 4


# ------------------------------------------------------- 라우트별 RAG 상한
@pytest.mark.parametrize(
    ("ext", "limit"),
    [
        (".md", 10 * MB),  # HWP 파싱 결과 · 텍스트 FILE_COPY
        (".txt", 10 * MB),
        (".html", 10 * MB),
        (".pptx", 10 * MB),  # Slides export · PPTX 복사
        (".docx", 50 * MB),
        (".pdf", 50 * MB),
        (".xlsx", 10 * MB),  # 문서에 상한 없음 → 보수적으로 낮은 쪽
        (".bin", 10 * MB),
    ],
)
def test_size_limit_matches_rag_engine_per_file_type(ext: str, limit: int) -> None:
    assert sync_main._size_limit_for(_Settings(), ext) == limit


def test_operator_limit_can_only_tighten_never_loosen() -> None:
    class _Tight(_Settings):
        max_gcs_bytes = 5 * MB

    # MAX_GCS_BYTES 는 운영 상한 — RAG 한도를 넘겨 올릴 수는 없어야 한다
    assert sync_main._size_limit_for(_Tight(), ".pdf") == 5 * MB
    assert sync_main._size_limit_for(_Settings(), ".pdf") == 50 * MB


def test_extension_lookup_is_case_insensitive() -> None:
    assert sync_main._size_limit_for(_Settings(), ".PDF") == 50 * MB


# ------------------------------------------------------- 실제 게이트 동작
class _GateStore:
    def __init__(self) -> None:
        self.split: list[tuple[str, int]] = []

    def should_reparse(self, *_a: Any, **_k: Any) -> bool:
        return True

    def get(self, _fid: str) -> None:
        return None

    def should_skip_reindex(self, *_a: Any, **_k: Any) -> bool:
        return False

    def upsert(self, _state: Any) -> None:
        pass

    def enqueue_split(self, file_id: str, _reason: str, size: int, **_f: Any) -> None:
        self.split.append((file_id, size))

    def enqueue_dlq(self, *_a: Any, **_k: Any) -> None:
        pass


class _GateGcs:
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload_normalized_md(self, _md: str, fid: str) -> str:
        self.uploaded.append(f"{fid}.md")
        return f"gs://norm/normalized/{fid}.md"

    def upload_bytes(self, _d: bytes, _b: str, blob: str, **_k: Any) -> str:
        self.uploaded.append(blob)
        return f"gs://norm/{blob}"

    def upload_path_sidecar_md(self, _md: str, fid: str) -> str:
        return f"gs://norm/normalized/{fid}.meta.md"


class _GateDrive:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def download_file(self, _fid: str) -> bytes:
        return self._payload

    def resolve_path_context(self, _fid: str, name: str):
        from shared.path_context import build_path_context

        return build_path_context(["f"], name)


def test_20mb_text_is_rejected_because_it_uploads_as_markdown() -> None:
    """텍스트는 breadcrumb 를 붙여 .md 로 올라간다 — 상한은 원본 MIME 이 아니라 .md 기준."""
    store, gcs = _GateStore(), _GateGcs()
    body = sync_main.IngestBody(
        fileId="f1", driveId="d", name="big.txt", mimeType="text/plain"
    )

    res = sync_main._ingest_file_copy(
        body, store, gcs, _GateDrive(b"x" * (20 * MB)), _Settings()
    )

    assert res["status"] == "SPLIT_QUEUED"
    assert store.split == [("f1", pytest.approx(20 * MB, abs=2000))]
    assert gcs.uploaded == [], "한도 초과분을 GCS 에 올려서는 안 된다"


def test_20mb_pdf_passes_because_pdf_limit_is_50mb() -> None:
    store, gcs = _GateStore(), _GateGcs()
    body = sync_main.IngestBody(
        fileId="f1", driveId="d", name="big.pdf", mimeType="application/pdf"
    )

    res = sync_main._ingest_file_copy(
        body, store, gcs, _GateDrive(b"x" * (20 * MB)), _Settings()
    )

    assert res["status"] == "GCS_READY"
    assert store.split == []


def test_20mb_pptx_is_rejected_even_though_it_is_under_max_gcs_bytes() -> None:
    # 구 구현은 단일 50MB 라 통과시킨 뒤 RAG import 에서 죽었다.
    store, gcs = _GateStore(), _GateGcs()
    body = sync_main.IngestBody(
        fileId="f1",
        driveId="d",
        name="deck.pptx",
        mimeType=(
            "application/vnd.openxmlformats-officedocument"
            ".presentationml.presentation"
        ),
    )

    res = sync_main._ingest_file_copy(
        body, store, gcs, _GateDrive(b"x" * (20 * MB)), _Settings()
    )

    assert res["status"] == "SPLIT_QUEUED"
    assert gcs.uploaded == []


# ------------------------------------------------------- #6 백필 스케일
def test_backfill_reuses_one_drive_client_per_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일마다 DriveClient 를 만들면 인증·discovery 가 파일 수만큼 돈다."""
    built: list[int] = []
    files = 24

    class _Drive:
        def __init__(self) -> None:
            built.append(threading.get_ident())

        def get_start_page_token(self, _d: str) -> str:
            return "tok"

        def iter_backfill_files(self, drive_id: str, _folders: list[str]):
            for i in range(files):
                yield {
                    "id": f"f{i}",
                    "driveId": drive_id,
                    "name": f"f{i}.txt",
                    "mimeType": "text/plain",
                }

    class _Store:
        def __init__(self) -> None:
            self.committed: list[tuple[str, str]] = []

        def try_acquire_lock(self, _n: str, *, ttl_seconds: int) -> bool:
            return True

        def release_lock(self, _n: str) -> None:
            pass

        def get_start_page_token(self, _d: str) -> None:
            return None

        def set_start_page_token(self, d: str, t: str) -> None:
            self.committed.append((d, t))

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", _Store)
    monkeypatch.setattr(sync_main, "DriveClient", _Drive)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(
        sync_main, "RagEngineClient", lambda: type("R", (), {"delete_files_by_ids": lambda _s, _i: 0})()
    )
    monkeypatch.setattr(sync_main, "_import_and_mark", lambda _s, uris, _i, **_k: list(uris))
    monkeypatch.setattr(
        sync_main,
        "_ingest_with",
        lambda body, **_c: {
            "status": "GCS_READY",
            "gcsUris": [f"gs://norm/normalized/{body.file_id}.txt"],
        },
    )

    sync_main.backfill_run(sync_main.BackfillRunBody(driveId="drive"))

    # 스냅샷용 1개 + 워커당 1개. 파일당 1개(24개)여서는 안 된다.
    assert len(built) <= 1 + _Settings.raw_upload_concurrency
    assert len(built) < files


def test_backfill_scans_the_corpus_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    deletes: list[list[str]] = []

    class _Drive:
        def get_start_page_token(self, _d: str) -> str:
            return "tok"

        def iter_backfill_files(self, drive_id: str, _folders: list[str]):
            for i in range(6):
                yield {
                    "id": f"f{i}",
                    "driveId": drive_id,
                    "name": f"f{i}.txt",
                    "mimeType": "text/plain",
                }

    class _Store:
        def try_acquire_lock(self, _n: str, *, ttl_seconds: int) -> bool:
            return True

        def release_lock(self, _n: str) -> None:
            pass

        def get_start_page_token(self, _d: str) -> None:
            return None

        def set_start_page_token(self, _d: str, _t: str) -> None:
            pass

        def get(self, file_id: str):
            return DocState(file_id=file_id, drive_id="drive")

        def upsert(self, _state) -> None:
            pass

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
            return 0

        def import_from_gcs(self, uris: list[str]) -> list[str]:
            return list(uris)

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", _Store)
    monkeypatch.setattr(sync_main, "DriveClient", _Drive)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(
        sync_main,
        "_ingest_with",
        lambda body, **_c: {
            "status": "GCS_READY",
            "gcsUris": [f"gs://norm/normalized/{body.file_id}.txt"],
        },
    )

    sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=2)
    )

    assert len(scans) == 1, f"코퍼스를 {len(scans)}회 순회했다 — 1회여야 한다"
    # 삭제 대상은 그 배치가 실제로 import 하는 파일뿐이어야 한다.
    assert sorted(fid for batch in deletes for fid in batch) == sorted(
        f"f{i}" for i in range(6)
    )
    assert all(len(batch) <= 2 for batch in deletes)


# ------------------------------------------------------- #8 search top_k
def test_search_still_returns_top_k_after_filtering_hidden_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자르고 거르면 결과가 모자란다 — 거르고 잘라야 한다."""
    import services.mcp_server.main as mcp_main
    from shared.models import SearchHit, SearchSource

    hits = [
        SearchHit(
            text=f"본문 {i}",
            score=0.1 * i,
            source=SearchSource(file_id=f"f{i}", name=f"f{i}.pdf"),
        )
        for i in range(10)
    ]
    # f0·f1 은 삭제/스킵된 문서 — 결과에서 빠져야 하지만 자리는 채워져야 한다
    hidden = {
        "f0": DocState(file_id="f0", drive_id="d", status=DocStatus.DELETED),
        "f1": DocState(file_id="f1", drive_id="d", status=DocStatus.SKIPPED),
    }

    class _Rag:
        def __init__(self, *_a: Any) -> None:
            pass

        def retrieve(self, _q: str, *, top_k: int) -> list[SearchHit]:
            return hits[:top_k]

    class _Store:
        def __init__(self, *_a: Any) -> None:
            pass

        def get(self, fid: str):
            return hidden.get(fid) or DocState(
                file_id=fid, drive_id="d", status=DocStatus.INDEXED
            )

    monkeypatch.setattr(mcp_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(mcp_main, "DocStateStore", _Store)

    results = mcp_main.search(query="q", top_k=5)

    assert len(results) == 5, f"top_k=5 인데 {len(results)}개만 반환됐다"
    assert [r["source"]["fileId"] for r in results] == ["f2", "f3", "f4", "f5", "f6"]
    assert [r["rank"] for r in results] == [1, 2, 3, 4, 5]


def test_search_fills_top_k_at_the_maximum_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """k 가 상한이면 여유분이 0 이라 필터가 걷어낸 자리를 못 채운다."""
    import services.mcp_server.main as mcp_main
    from shared.models import SearchHit, SearchSource

    k = mcp_main.MAX_TOP_K
    pool = [
        SearchHit(
            text=f"본문 {i}",
            score=0.01 * i,
            source=SearchSource(file_id=f"f{i}", name=f"f{i}.pdf"),
        )
        for i in range(k * 4)
    ]
    hidden = {
        f"f{i}": DocState(file_id=f"f{i}", drive_id="d", status=DocStatus.DELETED)
        for i in range(3)
    }

    class _Rag:
        def __init__(self, *_a: Any) -> None:
            pass

        def retrieve(self, _q: str, *, top_k: int) -> list[SearchHit]:
            return pool[:top_k]

    class _Store:
        def __init__(self, *_a: Any) -> None:
            pass

        def get(self, fid: str):
            return hidden.get(fid) or DocState(
                file_id=fid, drive_id="d", status=DocStatus.INDEXED
            )

    monkeypatch.setattr(mcp_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(mcp_main, "DocStateStore", _Store)

    results = mcp_main.search(query="q", top_k=k)

    assert len(results) == k, f"top_k={k} 인데 {len(results)}개만 반환됐다"
    assert all(r["source"]["fileId"] not in hidden for r in results)
