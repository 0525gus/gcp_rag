"""단위 테스트 — GCP 불필요."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.parser.cleanup import cleanup_markdown, to_nfc
from services.parser.quality_gate import ParseMetrics, evaluate_quality
from shared.config import Settings
from shared.hashing import sha256_text
from shared.mime_types import RouteKind, classify_route, is_hwp_family, is_hwpx


def test_nfc_normalization():
    assert to_nfc("\u1100\u1161") == "가"


def test_cleanup_strips_page_numbers():
    text = "본문입니다.\n\n1\n\n다음 문단\n페이지 2 / 10\n끝"
    out = cleanup_markdown(text)
    assert "본문입니다" in out
    assert "다음 문단" in out
    assert "페이지 2 / 10" not in out


def test_classify_hwp():
    assert classify_route("application/x-hwp", "a.hwp") == RouteKind.HWP_PARSE
    assert classify_route("application/haansofthwpx", "a.hwpx") == RouteKind.HWP_PARSE
    assert is_hwp_family("", "report.HWPX")
    assert is_hwpx("application/hwp+zip", "x.hwpx")


def test_classify_routes():
    assert classify_route("application/pdf", "a.pdf") == RouteKind.FILE_COPY
    assert (
        classify_route("application/vnd.google-apps.document", "doc")
        == RouteKind.GOOGLE_EXPORT
    )
    assert classify_route("application/octet-stream", "a.bin") == RouteKind.SKIP
    assert classify_route("", "", removed=True) == RouteKind.DELETE


def test_no_extractor_formats_are_not_skipped():
    """SKIP 은 GCS 업로드도 사이드카도 없다 — 파일명으로도 검색되지 않는다.

    본문을 못 뽑는 형식이라도 FILE_COPY 로 받아 경로 사이드카는 남긴다.
    """
    assert classify_route("application/zip", "a.zip") == RouteKind.FILE_COPY
    assert classify_route("application/vnd.ms-excel", "a.xls") == RouteKind.FILE_COPY


def test_macro_enabled_xlsx_reaches_the_spreadsheet_branch():
    """.xlsm 은 _SPREADSHEET_COPY_MIMES 에 있는데도 SKIP 되고 있었다."""
    from services.sync.main import _SPREADSHEET_COPY_MIMES  # noqa: PLC0415

    mime = "application/vnd.ms-excel.sheet.macroenabled.12"
    assert classify_route(mime, "a.xlsm") == RouteKind.FILE_COPY
    assert mime in _SPREADSHEET_COPY_MIMES


def test_sidecar_only_mimes_are_a_subset_of_file_copy():
    """사이드카 전용 목록에 넣었는데 FILE_COPY 가 아니면 SKIP 으로 새어 나간다."""
    from shared.mime_types import FILE_COPY_MIME, SIDECAR_ONLY_MIME  # noqa: PLC0415

    assert SIDECAR_ONLY_MIME <= FILE_COPY_MIME


def test_quality_gate_density():
    settings = Settings(
        gcp_project_id="p",
        gcs_raw_bucket="r",
        gcs_normalized_bucket="n",
        rag_corpus_name="c",
        qg_density_threshold=0.01,
    )
    low = ParseMetrics(text_length=10, source_bytes=100_000)
    gate = evaluate_quality(low, settings)
    assert gate.triggered


def test_quality_gate_relaxed_covers_corpus_fails():
    settings = Settings(
        gcp_project_id="p",
        gcs_raw_bucket="r",
        gcs_normalized_bucket="n",
        rag_corpus_name="c",
        qg_density_threshold=0.0005,
    )
    assert not evaluate_quality(
        ParseMetrics(text_length=2360, source_bytes=2_772_480), settings
    ).triggered
    assert not evaluate_quality(
        ParseMetrics(text_length=1051, source_bytes=1_165_824), settings
    ).triggered


def test_sha256_stable():
    assert sha256_text("hello").startswith("sha256:")


def test_path_breadcrumb_bundle():
    from shared.path_context import (
        build_breadcrumb_markdown,
        build_path_context,
        strip_breadcrumb,
    )

    ctx = build_path_context(
        ["컴공", "문서결재", "2026 digital training"],
        "안내.pdf",
    )
    assert ctx.bundle == "2026 digital training"
    assert ctx.path == "컴공/문서결재/2026 digital training/안내.pdf"

    md = build_breadcrumb_markdown(
        path=ctx.path,
        bundle=ctx.bundle,
        title="안내.pdf",
        body="# 본문\n내용",
    )
    # 제목·자료묶음은 한 번씩만. 경로는 싣지 않는다(거의 전 문서가 공유해 신호가 없음)
    assert md.count("안내.pdf") == 1
    assert md.count("2026 digital training") == 1
    assert "경로:" not in md
    assert "# 본문" in md and "내용" in md

    # 재파싱해도 머리말이 두 겹으로 쌓이지 않아야 한다
    again = build_breadcrumb_markdown(
        path=ctx.path,
        bundle=ctx.bundle,
        title="안내.pdf",
        body=md,
    )
    assert again == md
    assert "내용" in strip_breadcrumb(again)

    # GCS 에 남아있는 구형(YAML frontmatter) 산출물도 걷어내야 한다
    legacy = (
        "---\n"
        f"path: {ctx.path}\n"
        f"bundle: {ctx.bundle}\n"
        "title: 안내.pdf\n"
        "---\n\n"
        "# 안내.pdf\n\n"
        f"자료묶음: {ctx.bundle}\n"
        f"경로: {ctx.path}\n\n"
        "# 본문\n내용\n"
    )
    assert strip_breadcrumb(legacy).strip() == "# 본문\n내용"
    migrated = build_breadcrumb_markdown(
        path=ctx.path, bundle=ctx.bundle, title="안내.pdf", body=legacy
    )
    assert migrated.strip() == md.strip()

    # 본문이 그냥 `# 제목` 으로 시작하는 경우는 헤더로 오인해 먹으면 안 된다
    assert strip_breadcrumb("# 진짜 제목\n\n본문") == "# 진짜 제목\n\n본문"

    # sidecar 는 머리말이 유일한 내용이라 예전 그대로 둔다
    side = build_breadcrumb_markdown(
        path=ctx.path, bundle=ctx.bundle, title="슬라이드.pptx"
    )
    assert "관련 PDF/PPTX/HWP" in side
    assert "경로:" in side


def test_search_postprocess():
    from shared.models import SearchHit, SearchSource
    from shared.search_postprocess import (
        extract_file_id,
        postprocess_hits,
        unescape_chunk_text,
    )

    assert extract_file_id("1abc.pdf") == "1abc"
    assert extract_file_id("1abc.meta.md") == "1abc"
    assert extract_file_id("gs://b/normalized/1abc.md") == "1abc"
    assert extract_file_id("1abc.txt") == "1abc"
    assert "&lt;표1&gt;" not in unescape_chunk_text("아래 &lt;표1&gt; 참고")
    assert "표1" in unescape_chunk_text("아래 &lt;표1&gt; 참고")

    hits = [
        SearchHit(
            text="강의계획서 입력 개선안내 &lt;표1&gt;",
            score=0.237,
            source=SearchSource(file_id="a.pdf", name="a.pdf"),
        ),
        SearchHit(
            text="종합설계 의무상담 안내",
            score=0.191,
            source=SearchSource(file_id="b.pdf", name="b.pdf"),
        ),
        SearchHit(
            text="종합설계 의무상담 안내 중복",
            score=0.20,
            source=SearchSource(file_id="b.pdf", name="b.pdf"),
        ),
        SearchHit(
            text="강의계획서 입력 개선안내 <표1>",
            score=0.238,
            source=SearchSource(file_id="c.pdf", name="c.pdf"),
        ),
    ]
    out = postprocess_hits(hits, top_k=3)
    # 들어온 순서를 그대로 존중한다(83563ad). score 를 거리로 볼지 유사도로 볼지
    # 추측해 여기서 다시 정렬하면 추측이 틀렸을 때 순위가 통째로 뒤집힌다.
    # c 는 unescape 하면 a 와 본문이 같아진다 — 같은 글이 두 파일로 색인된
    # 경우라 뒤엣것을 버린다. 그래서 3칸을 요청해도 2건만 나온다.
    assert [h.source.file_id for h in out] == ["a", "b"]
    # 확장자는 떼고, 문서당 한 줄로 접는다
    assert len({h.source.file_id for h in out}) == len(out)
    # 같은 문서의 청크 둘은 버리지 않고 이어 붙인다
    assert "의무상담 안내" in out[1].text and "중복" in out[1].text
    assert "&lt;" not in out[0].text and "표1" in out[0].text


def test_folder_allowlist_ancestry():
    from shared.folder_scope import is_under_folder_allowlist

    tree = {
        "file1": ["sub"],
        "sub": ["board"],
        "board": ["drive_root"],
        "other": ["drive_root"],
        "drive_root": [],
    }

    def resolve(fid: str) -> list[str]:
        return tree.get(fid, [])

    assert is_under_folder_allowlist(
        file_id="file1",
        parents=["sub"],
        allowlist={"board"},
        resolve_parents=resolve,
    )
    assert not is_under_folder_allowlist(
        file_id="lonely",
        parents=["other"],
        allowlist={"board"},
        resolve_parents=resolve,
    )
    assert is_under_folder_allowlist(
        file_id="x",
        parents=["anywhere"],
        allowlist=set(),
        resolve_parents=resolve,
    )


def test_rhwp_parser_mocked(monkeypatch):
    fake_ir = MagicMock()
    fake_ir.to_markdown.return_value = "# 제목\n\n본문입니다.\n"
    fake_ir.blocks = []
    fake_doc = MagicMock()
    fake_doc.to_ir.return_value = fake_ir
    fake_rhwp = MagicMock()
    fake_rhwp.Document.from_bytes.return_value = fake_doc
    monkeypatch.setitem(sys.modules, "rhwp", fake_rhwp)

    from services.parser.rhwp_parser import parse_hwp_bytes

    result = parse_hwp_bytes(b"fake-bytes", filename="a.hwp")
    assert "본문입니다" in result.markdown
    assert result.engine == "rhwp"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
