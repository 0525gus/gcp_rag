from __future__ import annotations

from typing import Any

import pytest

import services.sync.main as sync_main
from services.parser.quality_gate import (
    ParseMetrics,
    count_markdown_tables,
    evaluate_quality,
)

MB = 1024 * 1024


class _QgSettings:
    qg_density_threshold = 0.0005
    qg_table_loss_ratio = 0.3
    qg_min_text_length = 20


def _metrics(**kw: Any) -> ParseMetrics:
    base = {"text_length": 5000, "source_bytes": 100_000}
    base.update(kw)
    return ParseMetrics(**base)


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("| a | b |\n| --- | --- |\n| 1 | 2 |", 1),
        ("| a |\n|---|\n| 1 |\n\n| c |\n| :--- |\n| 2 |", 2),
        ("<table><tr><td>x</td></tr></table>", 1),
        ("표가 없는 본문입니다.", 0),
        ("| 파이프만 있고 구분행이 없음 |", 0),
        ("", 0),
    ],
)
def test_count_markdown_tables(markdown: str, expected: int) -> None:
    assert count_markdown_tables(markdown) == expected


def test_g2_fires_when_tables_are_lost_in_conversion() -> None:
    g = evaluate_quality(_metrics(table_count=10, tables_rendered=2), _QgSettings())

    assert g.triggered
    assert any(r.startswith("G2_TABLE_LOST") for r in g.reasons), g.reasons


def test_g2_silent_when_all_tables_survive() -> None:
    g = evaluate_quality(_metrics(table_count=10, tables_rendered=10), _QgSettings())
    assert not g.triggered


def test_g2_tolerates_loss_under_threshold() -> None:
    g = evaluate_quality(_metrics(table_count=10, tables_rendered=8), _QgSettings())
    assert not g.triggered


def test_g2_never_fires_without_a_baseline() -> None:
    g = evaluate_quality(_metrics(table_count=0, tables_rendered=0), _QgSettings())
    assert not g.triggered


def test_g2_ignores_extra_tables_rendered() -> None:
    g = evaluate_quality(_metrics(table_count=2, tables_rendered=5), _QgSettings())
    assert not g.triggered


def test_removed_gates_are_gone_from_settings() -> None:
    from shared.config import Settings

    fields = Settings.__dataclass_fields__
    assert "qg_table_loss_ratio" in fields
    assert "qg_image_ratio" not in fields, "G3 는 계산 경로가 없어 제거했다"
    assert "qg_table_fail_ratio" not in fields, "셀 실패율은 판별 불가라 제거했다"


def test_g1_and_empty_text_still_live() -> None:
    assert any(
        r.startswith("G1_DENSITY")
        for r in evaluate_quality(_metrics(text_length=30), _QgSettings()).reasons
    )
    g = evaluate_quality(_metrics(text_length=10), _QgSettings())
    assert g.empty_text


class _Settings:
    sync_folder_id_list: list[str] = []
    student_folder_id_list: list[str] = []
    audience_split_enabled = False
    rag_corpus_name_student = ""
    max_gcs_bytes = 50 * MB
    gcs_source_bucket = "norm"
    gcs_hwp_original_bucket = "raw"


class _Store:
    def __init__(self) -> None:
        self.split: list[str] = []

    def enqueue_split(self, file_id: str, *_a: Any, **_k: Any) -> None:
        self.split.append(file_id)

    def enqueue_dlq(self, *_a: Any, **_k: Any) -> None:
        pass

    def should_skip_reindex(self, *_a: Any, **_k: Any) -> bool:
        return False

    def upsert(self, _s: Any) -> None:
        pass

    def get(self, _fid: str) -> Any:
        return None

    def clear_dlq(self, _fid: str) -> None:
        pass


class _CountingDrive:
    def __init__(self) -> None:
        self.downloads = 0

    def download_file(self, _fid: str, max_bytes: int | None = None) -> bytes:
        self.downloads += 1
        return b"x" * 1024

    def resolve_path_context(self, _fid: str, name: str):
        from shared.path_context import build_path_context

        return build_path_context(["f"], name)


class _Gcs:
    def upload_source_md(self, _md: str, fid: str) -> str:
        return f"gs://norm/{fid}.md"

    def upload_bytes(self, *_a: Any, **_k: Any) -> str:
        return "gs://norm/x"

    def upload_source_sidecar_md(self, _md: str, fid: str) -> str:
        return f"gs://norm/{fid}.meta.md"

    def upload_hwp_original(self, *_a: Any, **_k: Any) -> str:
        return "gs://x"


def test_oversized_pdf_is_rejected_without_downloading() -> None:
    store, drive = _Store(), _CountingDrive()
    body = sync_main.IngestBody(
        fileId="f1",
        driveId="d",
        name="huge.pdf",
        mimeType="application/pdf",
        sizeBytes=80 * MB,
    )

    res = sync_main._ingest_direct(body, store, _Gcs(), drive, _Settings())

    assert res["status"] == "SKIPPED"
    assert res["reason"].startswith("ADMIN_REVIEW_SIZE:")
    assert store.split == []
    assert drive.downloads == 0, "거를 파일을 먼저 메모리에 올렸다"


def test_oversized_text_is_measured_against_markdown_limit_before_download() -> None:
    store, drive = _Store(), _CountingDrive()
    body = sync_main.IngestBody(
        fileId="f1",
        driveId="d",
        name="huge.txt",
        mimeType="text/plain",
        sizeBytes=20 * MB,
    )

    res = sync_main._ingest_direct(body, store, _Gcs(), drive, _Settings())

    assert res["status"] == "SPLIT_QUEUED"
    assert drive.downloads == 0


def test_unknown_size_falls_through_to_the_post_download_check() -> None:
    store, drive = _Store(), _CountingDrive()
    body = sync_main.IngestBody(
        fileId="f1", driveId="d", name="a.pdf", mimeType="application/pdf"
    )

    res = sync_main._ingest_direct(body, store, _Gcs(), drive, _Settings())

    assert drive.downloads == 1
    assert res["status"] == "GCS_READY"


def test_hwp_source_is_capped_by_memory_limit_not_the_markdown_limit() -> None:
    store = _Store()

    under = sync_main.IngestBody(
        fileId="f1",
        driveId="d",
        name="doc.hwp",
        mimeType="application/x-hwp",
        sizeBytes=30 * MB,
        parserUrl="https://parser.example",
    )
    assert (
        sync_main._size_gate(
            store,
            _Settings(),
            under,
            under.size_bytes,
            splittable=True,
            ext=".hwp",
            limit=_Settings.max_gcs_bytes,
        )
        is None
    ), "30MB HWP 는 통과해야 한다"

    over = sync_main.IngestBody(
        fileId="f2",
        driveId="d",
        name="doc.hwp",
        mimeType="application/x-hwp",
        sizeBytes=80 * MB,
        parserUrl="https://parser.example",
    )
    gated = sync_main._size_gate(
        store,
        _Settings(),
        over,
        over.size_bytes,
        splittable=True,
        ext=".hwp",
        limit=_Settings.max_gcs_bytes,
    )
    assert gated is not None and gated["status"] == "SPLIT_QUEUED"


def test_drive_size_string_is_parsed() -> None:
    from shared.drive import parse_drive_size

    assert parse_drive_size("12345") == 12345
    assert parse_drive_size(None) is None
    assert parse_drive_size("") is None


class _HashUnchangedStore:
    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.touched: list[tuple[str, str | None]] = []

    def should_skip_reindex(self, *_a: Any, **_k: Any) -> bool:
        return True

    def upsert(self, state: Any) -> None:
        self.upserts.append(state)

    def touch_modified_time(self, file_id: str, modified_time: str | None) -> None:
        self.touched.append((file_id, modified_time))


def _hash_unchanged_drive():
    class _D:
        def download_file(self, _fid: str) -> bytes:
            return b"hello world"

        def export_file(self, _fid: str, _mime: str) -> bytes:
            return b"hello world"

        def resolve_path_context(self, _fid: str, name: str):
            from shared.path_context import build_path_context

            return build_path_context([], name)

    return _D()


@pytest.mark.parametrize(
    ("mime", "ingest_fn"),
    [
        ("text/plain", "_ingest_direct"),
        ("application/pdf", "_ingest_direct"),
        ("application/vnd.google-apps.document", "_ingest_google_export"),
    ],
)
def test_hash_unchanged_never_downgrades_indexed_state(mime: str, ingest_fn: str) -> None:
    import services.sync.main as sync_main

    store = _HashUnchangedStore()
    body = sync_main.IngestBody(
        fileId="f1",
        driveId="d",
        name="doc.txt",
        mimeType=mime,
        modifiedTime="2026-08-02T00:00:00Z",
    )

    res = getattr(sync_main, ingest_fn)(
        body, store, object(), _hash_unchanged_drive(), _Settings()
    )

    assert res["status"] == "HASH_UNCHANGED"
    assert store.upserts == [], "HASH_UNCHANGED 는 status 를 다시 쓰면 안 된다"
    assert store.touched == [("f1", "2026-08-02T00:00:00Z")]
