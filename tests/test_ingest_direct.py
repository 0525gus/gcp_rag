"""_ingest_direct (FILE_COPY 라우트) 분기 검증.

이 함수는 테스트가 없어서 결함 두 개가 운영 재색인까지 가서야 드러났다
(2026-07-29). 둘 다 크기 게이트였고, 아래 두 테스트로 고정한다.

  - 원본 xlsx 를 RAG 한도로 재서 27.7MB 문서를 통째로 잃은 건
  - 변환 결과가 한도를 넘겨도 잘라서 넣지 못하고 실패한 건
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openpyxl = pytest.importorskip("openpyxl")

import services.sync.main as sync_main  # noqa: E402
from services.sync.main import IngestBody, _ingest_direct  # noqa: E402
from shared.config import Settings  # noqa: E402
from shared.path_context import PathContext  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _Drive:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_file(self, file_id: str) -> bytes:
        return self.payload

    def resolve_path_context(self, file_id: str, fallback: str) -> PathContext:
        return PathContext(
            path="Drive/문서결재/시험묶음",
            bundle="시험묶음",
            segments=("Drive", "문서결재", "시험묶음"),
        )


class _Gcs:
    def __init__(self) -> None:
        self.md: list[tuple[str, str]] = []
        self.blobs: list[str] = []
        self.sidecars: list[str] = []
        self.deleted: list[str] = []

    def upload_source_md(self, markdown: str, file_id: str) -> str:
        self.md.append((file_id, markdown))
        return f"gs://nb/{file_id}.md"

    def upload_bytes(self, data, bucket, blob_name, content_type=None) -> str:  # noqa: ANN001
        self.blobs.append(blob_name)
        return f"gs://{bucket}/{blob_name}"

    def upload_source_sidecar_md(self, markdown: str, file_id: str) -> str:
        self.sidecars.append(file_id)
        return f"gs://nb/{file_id}.meta.md"

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


class _Store:
    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.split_queue: list[tuple[str, str, int]] = []
        self.dlq: list[tuple[str, str]] = []

    def should_skip_reindex(self, file_id: str, content_hash: str) -> bool:
        return False

    def upsert(self, state: Any) -> None:
        self.upserts.append(state)

    def enqueue_split(self, file_id: str, reason: str, size_bytes: int, **kw: Any) -> None:
        self.split_queue.append((file_id, reason, size_bytes))

    def enqueue_dlq(self, file_id: str, reason: str, **kw: Any) -> None:
        self.dlq.append((file_id, reason))


def _settings(**over: Any) -> Settings:
    base = {
        "gcp_project_id": "p",
        "gcs_source_bucket": "nb",
        "max_gcs_bytes": 50 * 1024 * 1024,
    }
    base.update(over)
    return Settings(**base)


def _body(name: str = "표.xlsx", mime: str = XLSX_MIME) -> IngestBody:
    return IngestBody(fileId="f1", driveId="d1", name=name, mimeType=mime)


def _run(payload: bytes, *, body: IngestBody | None = None, settings: Settings | None = None):
    gcs, store = _Gcs(), _Store()
    res = _ingest_direct(
        body or _body(), store, gcs, _Drive(payload), settings or _settings()
    )
    return res, gcs, store


# ---------------------------------------------------------------- xlsx 변환
def test_xlsx_is_converted_to_markdown_body() -> None:
    res, gcs, _ = _run(_xlsx([["부서", "담당자"], ["교무처", "홍길동"]]))

    assert res["status"] == "GCS_READY"
    assert res["gcsUris"] == ["gs://nb/f1.md"]
    assert len(gcs.md) == 1
    assert "| 교무처 | 홍길동 |" in gcs.md[0][1]
    # 본문이 생겼으니 사이드카는 만들지 않는다
    assert gcs.sidecars == []


def test_xlsx_body_drops_stale_sidecar() -> None:
    # 예전에 사이드카만 올려둔 문서가 있다. 남기면 청크가 두 벌 잡힌다
    _, gcs, _ = _run(_xlsx([["a"], ["b"]]))
    assert gcs.deleted == ["gs://nb/f1.meta.md"]


def test_unreadable_xlsx_falls_back_to_sidecar() -> None:
    # 암호 걸린 파일(OLE2). 색인 자체를 실패시키지 않고 기존 동작으로 떨어진다
    res, gcs, store = _run(b"\xd0\xcf\x11\xe0" + b"\x00" * 600)

    assert res["status"] == "GCS_READY"
    assert gcs.md == []
    assert gcs.sidecars == ["f1"]
    assert store.split_queue == [] and store.dlq == []


# ---------------------------------------------------------------- 크기 게이트
def test_large_xlsx_original_is_not_measured_against_rag_limit(monkeypatch) -> None:
    """원본 xlsx 는 RAG 로 가지 않는다 — 원본 크기로 떨어뜨리면 안 된다.

    회귀 고정: 27.7MB xlsx 가 원본 게이트에서 SPLIT_QUEUED 로 떨어져
    본문은 물론 사이드카로도 검색되지 않았다. 변환하면 통과하는 문서였다.

    실제 27MB xlsx 를 만드는 대신 변환을 대역으로 바꾼다 — 여기서 재는 건
    게이트 판정이지 변환 품질이 아니다.
    """
    monkeypatch.setattr(sync_main, "xlsx_to_markdown", lambda data: "| a |\n| --- |")
    # RAG 기본 한도(10MB)를 넘고 저장 상한(50MB)에는 못 미치는 원본
    fat = b"PK\x03\x04" + b"\x00" * (12 * 1024 * 1024)
    assert sync_main._effective_limit(_settings(), ".xlsx") == 10 * 1024 * 1024

    res, gcs, store = _run(fat)

    assert store.split_queue == [], "원본 크기로 떨어뜨리면 안 된다"
    assert res["status"] == "GCS_READY"
    assert gcs.md, "변환 결과가 색인되어야 한다"


def test_oversized_original_still_blocked_by_storage_cap(monkeypatch) -> None:
    # RAG 한도는 안 재도 우리 저장 상한은 지켜야 한다
    monkeypatch.setattr(sync_main, "xlsx_to_markdown", lambda data: "| a |\n| --- |")
    fat = b"PK\x03\x04" + b"\x00" * 5000

    res, _, store = _run(fat, settings=_settings(max_gcs_bytes=4000))

    assert res["status"] == "SPLIT_QUEUED"
    assert store.split_queue and "SIZE_EXCEEDED" in store.split_queue[0][1]


def test_converted_markdown_is_measured_against_rag_limit(monkeypatch) -> None:
    """RAG 로 올라가는 건 .md 다. 원본이 작아도 산출물이 넘치면 막아야 한다.

    저장 상한은 넉넉히 두어 원본 게이트가 아니라 **변환 후 게이트**가
    도는 것을 확인한다.
    """
    huge = "가" * (4 * 1024 * 1024)  # UTF-8 로 12MB → .md 한도(10MB) 초과
    monkeypatch.setattr(sync_main, "xlsx_to_markdown", lambda data: huge)
    payload = _xlsx([["a"], ["b"]])

    res, gcs, store = _run(payload, settings=_settings(max_gcs_bytes=50 * 1024 * 1024))

    assert res["status"] == "SPLIT_QUEUED"
    assert store.split_queue and "SIZE_EXCEEDED" in store.split_queue[0][1]
    assert gcs.md == [], "한도를 넘긴 산출물은 올리지 않는다"


# ---------------------------------------------------------------- 텍스트 경로
def test_text_file_keeps_breadcrumb_body_path() -> None:
    res, gcs, _ = _run(
        "본문 내용".encode(),
        body=_body(name="메모.txt", mime="text/plain"),
    )
    assert res["gcsUris"] == ["gs://nb/f1.md"]
    assert "본문 내용" in gcs.md[0][1]


def test_binary_file_keeps_copy_plus_sidecar(monkeypatch) -> None:
    # 이 테스트의 관심사는 일반 바이너리 업로드 계약이다. PDF 텍스트 판정은
    # 아래의 실제 blank PDF 테스트에서 별도로 고정한다.
    monkeypatch.setattr(sync_main, "_pdf_has_extractable_text", lambda _data: (True, None))
    res, gcs, _ = _run(
        b"%PDF-1.4 fake",
        body=_body(name="문서.pdf", mime="application/pdf"),
    )
    assert gcs.blobs == ["f1.pdf"]
    assert gcs.sidecars == ["f1"]
    assert gcs.md == []


def test_scanned_pdf_falls_back_to_sidecar() -> None:
    """텍스트 없는 스캔 PDF가 Vertex 배치 전체를 반복 실패시키면 안 된다."""
    from pypdf import PdfWriter  # noqa: PLC0415

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)

    res, gcs, store = _run(
        buf.getvalue(),
        body=_body(name="스캔본.pdf", mime="application/pdf"),
    )

    assert res["status"] == "GCS_READY"
    assert res["gcsUris"] == ["gs://nb/f1.meta.md"]
    assert gcs.blobs == []
    assert gcs.sidecars == ["f1"]
    assert "PDF_NO_EXTRACTABLE_TEXT" in (store.upserts[-1].error or "")


# ------------------------------------------------- 본문 추출 수단이 없는 형식
ZIP_MIME = "application/zip"
XLS_MIME = "application/vnd.ms-excel"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroenabled.12"


class _NoDownloadDrive(_Drive):
    """다운로드를 시도하면 즉시 실패시킨다 — 사이드카 경로는 받을 이유가 없다."""

    def download_file(self, file_id: str) -> bytes:
        raise AssertionError("사이드카 전용 형식은 파일을 내려받으면 안 된다")


def _run_no_download(body: IngestBody, settings: Settings | None = None):
    gcs, store = _Gcs(), _Store()
    res = _ingest_direct(body, store, gcs, _NoDownloadDrive(b""), settings or _settings())
    return res, gcs, store


@pytest.mark.parametrize("mime,name", [(ZIP_MIME, "매뉴얼.zip"), (XLS_MIME, "대상목록.xls")])
def test_no_extractor_formats_index_sidecar_only(mime: str, name: str) -> None:
    """SKIP 으로 두면 파일명으로도 검색되지 않는다 — 사이드카만이라도 남긴다."""
    res, gcs, store = _run_no_download(_body(name=name, mime=mime))

    assert gcs.sidecars == ["f1"]
    assert gcs.blobs == []  # 원본은 GCS 에 올리지 않는다 (RAG import 가 거부한다)
    assert gcs.md == []
    assert res["status"] == "GCS_READY"
    assert res["route"] == "FILE_COPY"
    state = store.upserts[-1]
    assert state.status.value != "SKIPPED"  # 검색단이 SKIPPED 를 걸러낸다
    assert "NO_BODY_EXTRACTOR" in (state.error or "")


def test_oversized_zip_is_not_queued_for_split() -> None:
    """RAG 한도(10MB)로 재면 큰 ZIP 이 SPLIT_QUEUED 로 영구 정체한다.

    올리지도 않을 원본 바이트 때문에 문서를 잃는, 이 함수가 이미 두 번 겪은 사고다.
    """
    body = IngestBody(
        fileId="f1", driveId="d1", name="매뉴얼.zip", mimeType=ZIP_MIME,
        sizeBytes=200 * 1024 * 1024,
    )
    res, gcs, store = _run_no_download(body)

    assert store.split_queue == []
    assert store.dlq == []
    assert gcs.sidecars == ["f1"]
    assert res["status"] == "GCS_READY"


def test_no_extractor_format_skips_reindex_when_hash_matches() -> None:
    class _Skipping(_Store):
        def __init__(self) -> None:
            super().__init__()
            self.touched: list[str] = []

        def should_skip_reindex(self, file_id: str, content_hash: str) -> bool:
            return True

        def touch_modified_time(self, file_id: str, modified_time) -> None:  # noqa: ANN001
            self.touched.append(file_id)

    gcs, store = _Gcs(), _Skipping()
    res = _ingest_direct(
        _body(name="매뉴얼.zip", mime=ZIP_MIME), store, gcs, _NoDownloadDrive(b""), _settings()
    )
    assert res["status"] == "HASH_UNCHANGED"
    assert gcs.sidecars == []
    assert store.touched == ["f1"]


def test_xlsm_body_is_converted_like_xlsx() -> None:
    """매크로 엑셀은 내부가 같은 OOXML 이라 그대로 읽힌다.

    _SPREADSHEET_COPY_MIMES 에는 진작 들어 있었는데 FILE_COPY_MIME 에 빠져 있어
    classify_route 가 SKIP 을 돌려주고 있었다 — 변환 코드에 도달조차 못 했다.
    """
    res, gcs, _ = _run(
        _xlsx([["부서", "담당자"], ["교무처", "홍길동"]]),
        body=_body(name="집계표.xlsm", mime=XLSM_MIME),
    )
    assert gcs.md, "본문 마크다운이 만들어져야 한다"
    _, markdown = gcs.md[-1]
    assert "홍길동" in markdown
    assert gcs.blobs == []  # 스프레드시트 원본은 색인 대상이 아니다
    assert res["status"] == "GCS_READY"


# ------------------------------------------------------------- PDF 분할 경로
def test_split_pdf_parts_are_not_measured_against_the_original_size(monkeypatch) -> None:
    """쪼갠 PDF 를 원본 전체 크기로 다시 재면 분할이 통째로 무의미해진다.

    split_pdf 가 파트마다 한도 이하로 만들어 놓아도, 사후 게이트가 원본 크기를
    보면 무조건 걸려 SPLIT_QUEUED 로 떨어진다. 그러면 분할 로직에 도달은 하되
    결과물이 하나도 업로드되지 않는다 — 로컬 종단 검증에서 실제로 잡힌 결함이다.
    """
    parts = [b"%PDF-1.4 part1", b"%PDF-1.4 part2"]
    monkeypatch.setattr(sync_main, "_pdf_has_extractable_text", lambda _data: (True, None))
    monkeypatch.setattr(sync_main, "split_pdf", lambda data, limit: parts)
    # 분할 트리거와 사후 게이트가 함께 쓰는 한도만 낮춘다.
    # 다운로드 전 게이트는 max_gcs_bytes 를 쓰므로 영향받지 않는다.
    monkeypatch.setattr(sync_main, "_effective_limit", lambda settings, ext: 4)

    res, gcs, store = _run(
        b"%PDF-1.4 " + b"x" * 200,
        body=_body(name="대용량.pdf", mime="application/pdf"),
    )

    assert store.split_queue == []
    assert res["status"] == "GCS_READY"
    assert len([b for b in gcs.blobs if ".part" in b]) == 2
    assert gcs.sidecars == ["f1"]


def test_unsplittable_oversized_pdf_still_goes_to_split_queue(monkeypatch) -> None:
    """분할에 실패하면 예전대로 큐로 보낸다 — 게이트를 통째로 없앤 게 아니다."""
    from shared.pdf_split import PdfSplitError  # noqa: PLC0415

    def _boom(data, limit):  # noqa: ANN001
        raise PdfSplitError("깨진 PDF")

    monkeypatch.setattr(sync_main, "_pdf_has_extractable_text", lambda _data: (True, None))
    monkeypatch.setattr(sync_main, "split_pdf", _boom)
    monkeypatch.setattr(sync_main, "_effective_limit", lambda settings, ext: 4)

    res, gcs, store = _run(
        b"%PDF-1.4 " + b"x" * 200,
        body=_body(name="깨진.pdf", mime="application/pdf"),
    )

    assert res["status"] == "SPLIT_QUEUED"
    assert len(store.split_queue) == 1
    assert gcs.blobs == []
