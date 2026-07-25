"""shared/ 순수 함수 계약 테스트 (GCP 클라이언트 불필요)."""

from __future__ import annotations

import pytest

from services.parser.cleanup import cleanup_markdown, strip_noise
from shared.drive import DriveClient
from shared.folder_scope import is_under_folder_allowlist
from shared.gcs import parse_gs_uri
from shared.mime_types import RouteKind, classify_route, is_hwp_family, is_hwpx
from shared.models import SearchHit, SearchSource
from shared.search_postprocess import (
    distance_to_relevance,
    extract_file_id,
    postprocess_hits,
    unescape_chunk_text,
)


# ---------------------------------------------------------------- gcs URI
def test_parse_gs_uri_splits_bucket_and_blob() -> None:
    assert parse_gs_uri("gs://my-bucket/normalized/f1.md") == (
        "my-bucket",
        "normalized/f1.md",
    )


@pytest.mark.parametrize(
    "bad",
    ["https://my-bucket/f.md", "gs://only-bucket", "gs:///blob"],
)
def test_parse_gs_uri_rejects_malformed(bad: str) -> None:
    # 잘못된 URI는 조용히 빈 값을 돌려주지 말고 터져야 한다
    with pytest.raises(ValueError):
        parse_gs_uri(bad)


# ---------------------------------------------------------------- folder scope
def test_allowlist_empty_allows_everything() -> None:
    assert is_under_folder_allowlist(
        file_id="f", parents=["p"], allowlist=set(), resolve_parents=lambda _: []
    )


def test_allowlist_matches_direct_parent() -> None:
    assert is_under_folder_allowlist(
        file_id="f",
        parents=["allowed"],
        allowlist={"allowed"},
        resolve_parents=lambda _: [],
    )


def test_allowlist_matches_grandparent() -> None:
    tree = {"child": ["root"], "root": []}
    assert is_under_folder_allowlist(
        file_id="f",
        parents=["child"],
        allowlist={"root"},
        resolve_parents=lambda pid: tree.get(pid, []),
    )


def test_allowlist_rejects_outside_tree() -> None:
    tree = {"other": []}
    assert not is_under_folder_allowlist(
        file_id="f",
        parents=["other"],
        allowlist={"root"},
        resolve_parents=lambda pid: tree.get(pid, []),
    )


def test_allowlist_survives_parent_cycle() -> None:
    # a→b→a 순환에서도 무한 루프 없이 False 반환
    tree = {"a": ["b"], "b": ["a"]}
    assert not is_under_folder_allowlist(
        file_id="f",
        parents=["a"],
        allowlist={"root"},
        resolve_parents=lambda pid: tree.get(pid, []),
    )


def test_allowlist_matches_file_id_itself() -> None:
    assert is_under_folder_allowlist(
        file_id="folderX",
        parents=[],
        allowlist={"folderX"},
        resolve_parents=lambda _: [],
    )


# ---------------------------------------------------------------- cleanup
@pytest.mark.parametrize("line", ["- 3 -", "3 -", "12", "- 3", "iv"])
def test_strip_noise_removes_page_numbers(line: str) -> None:
    assert strip_noise(f"본문 시작\n{line}\n본문 끝") == "본문 시작\n본문 끝"


@pytest.mark.xfail(
    reason="_PAGE_NO가 ASCII '-'만 매칭 — cleanup.py 주석의 '— iv —' 사례는 미처리. "
    "고치면 파서 출력이 바뀌어 contentHash 변동 → 전체 재색인 유발되므로 별도 판단 필요.",
    strict=True,
)
@pytest.mark.parametrize("line", ["— iv —", "– 3 –"])
def test_strip_noise_removes_emdash_page_numbers(line: str) -> None:
    assert strip_noise(f"본문 시작\n{line}\n본문 끝") == "본문 시작\n본문 끝"


@pytest.mark.parametrize("line", ["2024", "did", "civil", "1,200원"])
def test_strip_noise_keeps_body_lines(line: str) -> None:
    # 연도·영단어·금액은 페이지번호로 오탐하면 안 됨
    assert line in strip_noise(f"본문\n{line}\n끝")


def test_strip_noise_removes_page_x_of_y() -> None:
    assert strip_noise("본문\nPage 3 of 10\n페이지 2 / 8\n끝") == "본문\n끝"


def test_strip_noise_collapses_blank_runs() -> None:
    assert strip_noise("a\n\n\n\n\nb") == "a\n\nb"


def test_cleanup_markdown_applies_nfc_then_strip() -> None:
    assert cleanup_markdown("한\n- 3 -\n") == "한"


# ---------------------------------------------------------------- mime routing
@pytest.mark.parametrize(
    "mime,name,expected",
    [
        ("application/x-hwp", "a.hwp", RouteKind.HWP_PARSE),
        ("", "보고서.hwpx", RouteKind.HWP_PARSE),
        ("application/vnd.google-apps.document", "doc", RouteKind.GOOGLE_EXPORT),
        ("application/pdf", "a.pdf", RouteKind.FILE_COPY),
        ("image/png", "a.png", RouteKind.SKIP),
        ("application/vnd.google-apps.folder", "folder", RouteKind.SKIP),
    ],
)
def test_classify_route(mime: str, name: str, expected: RouteKind) -> None:
    assert classify_route(mime, name) == expected


def test_classify_route_removed_wins_over_mime() -> None:
    assert classify_route("application/pdf", "a.pdf", removed=True) == RouteKind.DELETE


def test_is_hwp_family_is_case_insensitive() -> None:
    assert is_hwp_family("APPLICATION/X-HWP", "")
    assert is_hwp_family("", "REPORT.HWP")


def test_is_hwpx_distinguishes_from_hwp() -> None:
    assert is_hwpx("application/hwpx", "")
    assert is_hwpx("", "a.hwpx")
    assert not is_hwpx("application/x-hwp", "a.hwp")


# ---------------------------------------------------------------- search postprocess
@pytest.mark.parametrize(
    "display,expected",
    [
        ("abc123.md", "abc123"),
        ("abc123.meta.md", "abc123"),
        ("normalized/abc123.pdf", "abc123"),
        ("gs://b/normalized/abc123.docx", "abc123"),
        ("noext", "noext"),
    ],
)
def test_extract_file_id(display: str, expected: str) -> None:
    assert extract_file_id(display) == expected


def test_extract_file_id_falls_back_to_source_uri() -> None:
    assert extract_file_id("", "gs://b/normalized/xyz.md") == "xyz"


def test_distance_to_relevance_is_monotonic_decreasing() -> None:
    # 거리가 멀수록 relevance는 낮아야 함
    assert distance_to_relevance(0.0) > distance_to_relevance(0.5)
    assert distance_to_relevance(0.5) > distance_to_relevance(1.0)


def test_distance_to_relevance_clamps_range() -> None:
    assert distance_to_relevance(-1.0) == 0.0
    assert 0.0 <= distance_to_relevance(0.0) <= 1.0
    assert 0.0 <= distance_to_relevance(100.0) <= 1.0


def test_unescape_chunk_text_normalizes_entities_and_newlines() -> None:
    assert unescape_chunk_text("a &amp; b\r\n\r\n\r\n\r\nc") == "a & b\n\nc"


def _hit(fid: str, text: str, score: float) -> SearchHit:
    return SearchHit(text=text, score=score, source=SearchSource(file_id=fid))


def test_postprocess_preserves_vertex_order() -> None:
    """score 로 재정렬하면 안 된다.

    거리/유사도 여부를 추측해 뒤집는 방식이라, 유사도였을 경우 순위가 통째로
    거꾸로 뒤집힌다. Vertex 응답은 이미 관련도 순이므로 그대로 둔다.
    """
    hits = [_hit("f1.md", "가장 관련", 0.9), _hit("f2.md", "덜 관련", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert [h.text for h in out] == ["가장 관련", "덜 관련"]


def test_postprocess_keeps_first_chunk_per_file() -> None:
    # 중복 제거는 API 순서상 앞선 것(= 더 관련 있는 것)을 남긴다
    hits = [_hit("f1.md", "첫 청크", 0.9), _hit("f1.md", "둘째 청크", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert len(out) == 1
    assert out[0].text == "첫 청크"
    assert out[0].source.file_id == "f1"


def test_postprocess_passes_score_through_unchanged() -> None:
    # 점수는 표시용 원값 — 정규화하지 않는다
    out = postprocess_hits([_hit("f1.md", "본문", 0.42)], top_k=5)
    assert out[0].score == 0.42


def test_postprocess_dedups_near_identical_text_across_files() -> None:
    hits = [_hit("f1.md", "동일한 본문입니다", 0.1), _hit("f2.md", "동일한  본문입니다", 0.2)]
    assert len(postprocess_hits(hits, top_k=5)) == 1


def test_postprocess_respects_top_k() -> None:
    hits = [_hit(f"f{i}.md", f"본문 {i}", 0.1 * i) for i in range(1, 6)]
    assert len(postprocess_hits(hits, top_k=2)) == 2


# ---------------------------------------------------------------- drive change mapping
def test_to_change_maps_file_metadata() -> None:
    change = DriveClient._to_change(
        {
            "fileId": "f1",
            "removed": False,
            "file": {
                "id": "f1",
                "name": "보고서.hwp",
                "mimeType": "application/x-hwp",
                "modifiedTime": "2026-01-02T03:04:05Z",
                "trashed": False,
                "webViewLink": "https://drive.google.com/f1",
                "parents": ["p1"],
                "driveId": "d-from-file",
            },
        },
        "d-fallback",
    )
    assert change.file_id == "f1"
    assert change.drive_id == "d-from-file"
    assert change.name == "보고서.hwp"
    assert change.removed is False
    assert change.parents == ["p1"]


def test_to_change_treats_trashed_as_removed() -> None:
    change = DriveClient._to_change(
        {"fileId": "f1", "removed": False, "file": {"id": "f1", "trashed": True}},
        "d",
    )
    assert change.removed is True
    assert change.trashed is True


def test_to_change_handles_removed_without_file_payload() -> None:
    # removed 이벤트는 file 메타가 아예 없을 수 있음 → driveId는 인자로 폴백
    change = DriveClient._to_change({"fileId": "f1", "removed": True}, "d")
    assert change.file_id == "f1"
    assert change.drive_id == "d"
    assert change.removed is True
