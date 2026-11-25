from __future__ import annotations

import pytest

from services.parser.cleanup import cleanup_markdown, strip_noise
from shared.drive import DriveClient
from shared.folder_scope import is_under_folder_allowlist
from shared.gcs import parse_gs_uri
from shared.mime_types import RouteKind, classify_route, is_hwp_family, is_hwpx
from shared.models import SearchHit, SearchSource
from shared.lexical_rerank import query_terms, term_coverage
from shared.search_postprocess import (
    citation_label,
    extract_file_id,
    postprocess_hits,
    unescape_chunk_text,
)


def test_parse_gs_uri_splits_bucket_and_blob() -> None:
    assert parse_gs_uri("gs://my-bucket/f1.md") == (
        "my-bucket",
        "f1.md",
    )


@pytest.mark.parametrize(
    "bad",
    ["https://my-bucket/f.md", "gs://only-bucket", "gs:///blob"],
)
def test_parse_gs_uri_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_gs_uri(bad)


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


def test_allowlist_parent_lookup_failure_is_not_out_of_scope() -> None:
    def fail(_file_id: str) -> list[str]:
        raise RuntimeError("Drive unavailable")

    with pytest.raises(RuntimeError, match="cannot determine folder scope"):
        is_under_folder_allowlist(
            file_id="f",
            parents=["unknown-parent"],
            allowlist={"root"},
            resolve_parents=fail,
        )


def test_drive_scope_initial_lookup_failure_propagates() -> None:
    client = object.__new__(DriveClient)
    client.get_parents = lambda _file_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("Drive unavailable")
    )

    with pytest.raises(RuntimeError, match="Drive unavailable"):
        client.is_in_sync_scope("f", ["root"])


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
    assert line in strip_noise(f"본문\n{line}\n끝")


def test_strip_noise_removes_page_x_of_y() -> None:
    assert strip_noise("본문\nPage 3 of 10\n페이지 2 / 8\n끝") == "본문\n끝"


def test_strip_noise_collapses_blank_runs() -> None:
    assert strip_noise("a\n\n\n\n\nb") == "a\n\nb"


def test_cleanup_markdown_applies_nfc_then_strip() -> None:
    assert cleanup_markdown("한\n- 3 -\n") == "한"


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


@pytest.mark.parametrize(
    "display,expected",
    [
        ("abc123.md", "abc123"),
        ("abc123.meta.md", "abc123"),
        ("abc123.pdf", "abc123"),
        ("gs://b/abc123.docx", "abc123"),
        ("gs://b/abc123.doc", "abc123"),
        ("noext", "noext"),
        ("abc123.hwp", "abc123"),
        ("abc123.hwpx", "abc123"),
        ("abc123.doc", "abc123"),
        ("abc123.part12.pdf", "abc123"),
    ],
)
def test_extract_file_id(display: str, expected: str) -> None:
    assert extract_file_id(display) == expected


def test_extract_file_id_falls_back_to_source_uri() -> None:
    assert extract_file_id("", "gs://b/xyz.md") == "xyz"


def test_unescape_chunk_text_normalizes_entities_and_newlines() -> None:
    assert unescape_chunk_text("a &amp; b\r\n\r\n\r\n\r\nc") == "a & b\n\nc"


def _hit(fid: str, text: str, score: float) -> SearchHit:
    return SearchHit(text=text, score=score, source=SearchSource(file_id=fid))


def test_postprocess_preserves_vertex_order() -> None:
    hits = [_hit("f1.md", "가장 관련", 0.9), _hit("f2.md", "덜 관련", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert [h.text for h in out] == ["가장 관련", "덜 관련"]


def test_postprocess_collapses_a_file_into_one_result() -> None:
    hits = [_hit("f1.md", "첫 청크", 0.9), _hit("f1.md", "둘째 청크", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert len(out) == 1
    assert out[0].text.startswith("첫 청크")
    assert out[0].source.file_id == "f1"


def test_postprocess_max_chunks_per_file_one_keeps_only_the_first() -> None:
    hits = [_hit("f1.md", "첫 청크", 0.9), _hit("f1.md", "둘째 청크", 0.1)]
    out = postprocess_hits(hits, top_k=5, max_chunks_per_file=1)
    assert [h.text for h in out] == ["첫 청크"]


def test_postprocess_passes_score_through_unchanged() -> None:
    out = postprocess_hits([_hit("f1.md", "본문", 0.42)], top_k=5)
    assert out[0].score == 0.42


def test_postprocess_dedups_near_identical_text_across_files() -> None:
    hits = [_hit("f1.md", "동일한 본문입니다", 0.1), _hit("f2.md", "동일한  본문입니다", 0.2)]
    assert len(postprocess_hits(hits, top_k=5)) == 1


def test_postprocess_respects_top_k() -> None:
    hits = [_hit(f"f{i}.md", f"본문 {i}", 0.1 * i) for i in range(1, 6)]
    assert len(postprocess_hits(hits, top_k=2)) == 2


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
    change = DriveClient._to_change({"fileId": "f1", "removed": True}, "d")
    assert change.file_id == "f1"
    assert change.drive_id == "d"
    assert change.removed is True


def test_query_terms_drops_quotes_short_tokens_and_duplicates() -> None:
    assert query_terms('"LMS" "명단" 및 AI, LMS') == ["LMS", "명단", "AI"]


def test_query_terms_excludes_bigrams() -> None:
    assert query_terms("교수학습개발센터") == ["교수학습개발센터"]


def test_term_coverage_matches_hangul_with_josa_attached() -> None:
    matched, missing = term_coverage(
        ["교수학습개발센터", "명단"], "교수학습개발센터의 센터장으로 임명함"
    )
    assert matched == ["교수학습개발센터"]
    assert missing == ["명단"]


def test_term_coverage_requires_whole_token_for_latin() -> None:
    matched, missing = term_coverage(["AI"], "he said the train arrived")
    assert matched == []
    assert missing == ["AI"]

    matched, _ = term_coverage(["AI"], "AI혁신위원회 위원")
    assert matched == ["AI"]


def test_citation_label_prefixes_bundle_when_filename_is_generic() -> None:
    label = citation_label(
        {"name": "content.txt", "bundle": "147294_수강정정기간 안내", "fileId": "f1"}
    )
    assert label == "147294_수강정정기간 안내 / content.txt"


def test_citation_label_disambiguates_attachments_of_one_post() -> None:
    same = {"bundle": "147294_개강 안내", "fileId": "f1"}
    a = citation_label({**same, "name": "매뉴얼_pc.pdf"})
    b = citation_label({**same, "name": "매뉴얼_mobile.pdf"})
    assert a.startswith("147294_개강 안내 / ") and b.startswith("147294_개강 안내 / ")
    assert a != b


def test_citation_label_does_not_repeat_bundle_already_in_filename() -> None:
    label = citation_label(
        {"name": "2026년 안전점검의 날 운영계획.hwp", "bundle": "2026년 안전점검의 날", "fileId": "f1"}
    )
    assert label == "2026년 안전점검의 날 운영계획.hwp"


def test_citation_label_falls_back_to_path_without_bundle() -> None:
    label = citation_label(
        {"name": "content.txt", "bundle": "", "path": "Drive/공지/글제목/content.txt"}
    )
    assert label == "Drive/공지/글제목/content.txt"


def test_citation_label_keeps_plain_filename_when_nothing_to_add() -> None:
    assert citation_label({"name": "인사발령.hwp", "bundle": ""}) == "인사발령.hwp"
    assert citation_label({"name": "", "bundle": "", "fileId": "abc"}) == "abc"


def _multi(fid: str, n: int) -> list[SearchHit]:
    return [_hit(fid, f"{fid}-청크{i}", 0.1 * i) for i in range(1, n + 1)]


def test_total_budget_defaults_to_previous_behaviour() -> None:
    hits = _multi("f1.md", 3) + _multi("f2.md", 3)
    out = postprocess_hits(hits, top_k=5, max_chunks_per_file=3)
    assert out[0].text.count("[...]") == 2
    assert out[1].text.count("[...]") == 2


def test_total_budget_caps_response_size() -> None:
    hits = _multi("f1.md", 3) + _multi("f2.md", 3) + _multi("f3.md", 3)
    out = postprocess_hits(
        hits, top_k=3, max_chunks_per_file=3, max_total_chunks=4
    )
    assert len(out) == 3
    total = sum(h.text.count("[...]") + 1 for h in out)
    assert total == 4
    assert out[0].text.count("[...]") == 1


def test_total_budget_caps_document_count_when_top_k_exceeds_budget() -> None:
    hits = _multi("f1.md", 3) + _multi("f2.md", 3) + _multi("f3.md", 3)
    out = postprocess_hits(
        hits, top_k=3, max_chunks_per_file=3, max_total_chunks=1
    )
    assert [h.source.file_id for h in out] == ["f1"]
    assert all("[...]" not in h.text for h in out)


def test_total_budget_spreads_before_deepening() -> None:
    hits = _multi("f1.md", 3) + _multi("f2.md", 3)
    out = postprocess_hits(
        hits, top_k=2, max_chunks_per_file=3, max_total_chunks=4
    )
    assert [h.text.count("[...]") for h in out] == [1, 1]


def test_mcp_refuses_to_serve_without_any_authentication(monkeypatch) -> None:
    import services.mcp_server.main as mcp_main

    monkeypatch.setattr(mcp_main, "MCP_API_KEY", "")
    monkeypatch.setattr(mcp_main, "MCP_ALLOW_NO_AUTH", False)
    with pytest.raises(RuntimeError, match="without authentication"):
        mcp_main.build_app()


def test_mcp_allows_no_auth_only_as_an_explicit_opt_in(monkeypatch) -> None:
    import services.mcp_server.main as mcp_main

    monkeypatch.setattr(mcp_main, "MCP_API_KEY", "")
    monkeypatch.setattr(mcp_main, "MCP_ALLOW_NO_AUTH", True)
    assert mcp_main.build_app() is not None
