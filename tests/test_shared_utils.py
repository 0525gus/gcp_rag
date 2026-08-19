"""shared/ 순수 함수 계약 테스트 (GCP 클라이언트 불필요)."""

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
    build_answer_payload,
    citation_label,
    extract_file_id,
    postprocess_hits,
    unescape_chunk_text,
)


# ---------------------------------------------------------------- gcs URI
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
        ("abc123.pdf", "abc123"),
        ("gs://b/abc123.docx", "abc123"),
        ("gs://b/abc123.doc", "abc123"),
        ("noext", "noext"),
        # 파이프라인이 실제로 만드는 확장자인데 목록에 빠져 있었다.
        # 빠지면 fileId 로 안 접혀 코퍼스 삭제가 대상을 못 찾고,
        # _clean_file_ids 는 점 때문에 malformed 로 버린다.
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
    """score 로 재정렬하면 안 된다.

    거리/유사도 여부를 추측해 뒤집는 방식이라, 유사도였을 경우 순위가 통째로
    거꾸로 뒤집힌다. Vertex 응답은 이미 관련도 순이므로 그대로 둔다.
    """
    hits = [_hit("f1.md", "가장 관련", 0.9), _hit("f2.md", "덜 관련", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert [h.text for h in out] == ["가장 관련", "덜 관련"]


def test_postprocess_collapses_a_file_into_one_result() -> None:
    # 같은 문서의 청크는 몇 개를 이어 붙이든 결과 항목 하나로 접힌다.
    # 앞선 청크(= 더 관련 있는 것)가 머리에 오고, 출처는 그 청크의 것을 쓴다.
    hits = [_hit("f1.md", "첫 청크", 0.9), _hit("f1.md", "둘째 청크", 0.1)]
    out = postprocess_hits(hits, top_k=5)
    assert len(out) == 1
    assert out[0].text.startswith("첫 청크")
    assert out[0].source.file_id == "f1"


def test_postprocess_max_chunks_per_file_one_keeps_only_the_first() -> None:
    # 문서당 1청크로 조이면 예전 동작 그대로 — 둘째 청크는 버린다
    hits = [_hit("f1.md", "첫 청크", 0.9), _hit("f1.md", "둘째 청크", 0.1)]
    out = postprocess_hits(hits, top_k=5, max_chunks_per_file=1)
    assert [h.text for h in out] == ["첫 청크"]


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


# ------------------------------------------------- 질의어 커버리지 (근거 표시)
def test_query_terms_drops_quotes_short_tokens_and_duplicates() -> None:
    # 따옴표·조사 잔여물은 토큰이 아니고, 1글자는 노이즈라 버린다
    assert query_terms('"LMS" "명단" 및 AI, LMS') == ["LMS", "명단", "AI"]


def test_query_terms_excludes_bigrams() -> None:
    # 호출 LLM 에게 그대로 보여줄 값이라 '교수학' 같은 조각이 섞이면 안 된다
    assert query_terms("교수학습개발센터") == ["교수학습개발센터"]


def test_term_coverage_matches_hangul_with_josa_attached() -> None:
    matched, missing = term_coverage(
        ["교수학습개발센터", "명단"], "교수학습개발센터의 센터장으로 임명함"
    )
    assert matched == ["교수학습개발센터"]
    assert missing == ["명단"]


def test_term_coverage_requires_whole_token_for_latin() -> None:
    # 부분문자열로 보면 'ai' 가 said/train 에 걸려 근거 없는 매치가 된다
    matched, missing = term_coverage(["AI"], "he said the train arrived")
    assert matched == []
    assert missing == ["AI"]

    matched, _ = term_coverage(["AI"], "AI혁신위원회 위원")
    assert matched == ["AI"]


def _chunk(name: str, text: str, matched: list[str], missing: list[str]) -> dict:
    return {
        "text": text,
        "matchedTerms": matched,
        "missingTerms": missing,
        "source": {"fileId": "f-" + name, "name": name, "sourceUri": None},
    }


def test_build_answer_payload_labels_each_document_block() -> None:
    chunks = [
        _chunk("인사발령.hwp", "김나영 센터장", ["교수학습개발센터"], ["LMS"]),
        _chunk("조사표.xlsx", "AI 튜터 기능", ["LMS"], ["교수학습개발센터"]),
    ]
    out = build_answer_payload(chunks, "LMS 교수학습개발센터")
    # 라벨이 없으면 어느 문장이 어느 문서에서 왔는지 복원할 수 없다
    assert "[1] 인사발령.hwp\n김나영 센터장" in out["context"]
    assert "[2] 조사표.xlsx\nAI 튜터 기능" in out["context"]
    assert [c["n"] for c in out["citations"]] == [1, 2]


def test_citation_label_prefixes_bundle_when_filename_is_generic() -> None:
    """게시판 수집물은 파일명이 전부 content.txt — 제목은 자료묶음에만 있다.

    실측 1,155건 중 27건이고 코퍼스에서 가장 흔한 파일명이다. 라벨이
    `content.txt` 로만 나가면 여러 공지가 동시에 걸렸을 때 구분되지 않는다.
    """
    label = citation_label(
        {"name": "content.txt", "bundle": "147294_수강정정기간 안내", "fileId": "f1"}
    )
    assert label == "147294_수강정정기간 안내 / content.txt"


def test_citation_label_disambiguates_attachments_of_one_post() -> None:
    # 같은 게시글의 다른 첨부인지 다른 게시글인지 파일명으로는 알 수 없다
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
    # 자료묶음이 없는데 파일명도 기계가 붙인 것이면 경로가 유일한 단서다
    label = citation_label(
        {"name": "content.txt", "bundle": "", "path": "Drive/공지/글제목/content.txt"}
    )
    assert label == "Drive/공지/글제목/content.txt"


def test_citation_label_keeps_plain_filename_when_nothing_to_add() -> None:
    assert citation_label({"name": "인사발령.hwp", "bundle": ""}) == "인사발령.hwp"
    assert citation_label({"name": "", "bundle": "", "fileId": "abc"}) == "abc"


def test_build_answer_payload_flags_partial_when_terms_split_across_docs() -> None:
    chunks = [
        _chunk("인사발령.hwp", "김나영", ["교수학습개발센터"], ["LMS", "명단"]),
        _chunk("조사표.xlsx", "AI 튜터", ["LMS"], ["교수학습개발센터", "명단"]),
    ]
    out = build_answer_payload(chunks, '"LMS" "명단" "교수학습개발센터"')
    # 두 문서를 이어 붙여야만 질의가 덮인다 = 근거 없는 결합 위험
    assert out["coverage"] == "partial"
    # 어느 문서에도 없는 검색어는 되물어야 한다는 신호
    assert out["uncoveredTerms"] == ["명단"]


def test_build_answer_payload_reports_full_only_when_one_doc_covers_all() -> None:
    chunks = [
        _chunk("규정.hwp", "연구윤리 규정 개정", ["연구윤리", "개정"], []),
        _chunk("붙임.hwp", "참고", ["연구윤리"], ["개정"]),
    ]
    out = build_answer_payload(chunks, "연구윤리 개정")
    assert out["coverage"] == "full"
    assert out["uncoveredTerms"] == []


def test_build_answer_payload_handles_empty_result() -> None:
    out = build_answer_payload([], "LMS 명단")
    assert out["coverage"] == "none"
    assert out["context"] == ""
    assert out["chunk_count"] == 0
    assert out["uncoveredTerms"] == ["LMS", "명단"]


# ------------------------------------------------- 응답 총량 예산 (토큰 폭발 방지)
def _multi(fid: str, n: int) -> list[SearchHit]:
    return [_hit(fid, f"{fid}-청크{i}", 0.1 * i) for i in range(1, n + 1)]


def test_total_budget_defaults_to_previous_behaviour() -> None:
    # 예산을 안 주면 예전처럼 문서당 max_chunks_per_file 만큼 이어 붙인다
    hits = _multi("f1.md", 3) + _multi("f2.md", 3)
    out = postprocess_hits(hits, top_k=5, max_chunks_per_file=3)
    assert out[0].text.count("[...]") == 2
    assert out[1].text.count("[...]") == 2


def test_total_budget_caps_response_size() -> None:
    hits = _multi("f1.md", 3) + _multi("f2.md", 3) + _multi("f3.md", 3)
    out = postprocess_hits(
        hits, top_k=3, max_chunks_per_file=3, max_total_chunks=4
    )
    # 문서 3건은 그대로 나오고, 총 청크는 4개를 넘지 않는다
    assert len(out) == 3
    total = sum(h.text.count("[...]") + 1 for h in out)
    assert total == 4
    # 남은 예산은 관련도 1위 문서가 먼저 가져간다
    assert out[0].text.count("[...]") == 1


def test_total_budget_never_drops_a_document() -> None:
    # 예산이 문서 수보다 작아도 문서당 1청크는 보장 — 다양성이 이 함수의 목적
    hits = _multi("f1.md", 3) + _multi("f2.md", 3) + _multi("f3.md", 3)
    out = postprocess_hits(
        hits, top_k=3, max_chunks_per_file=3, max_total_chunks=1
    )
    assert [h.source.file_id for h in out] == ["f1", "f2", "f3"]
    assert all("[...]" not in h.text for h in out)


def test_total_budget_spreads_before_deepening() -> None:
    # 앞 문서에 몰아주지 않고 한 개씩 돌려 나눈다
    hits = _multi("f1.md", 3) + _multi("f2.md", 3)
    out = postprocess_hits(
        hits, top_k=2, max_chunks_per_file=3, max_total_chunks=4
    )
    assert [h.text.count("[...]") for h in out] == [1, 1]


# ------------------------------------------------- MCP 무인증 기동 거부
def test_mcp_refuses_to_serve_without_any_authentication(monkeypatch) -> None:
    """키가 없으면 미들웨어가 통째로 무력화된다 — 조용히 열리지 말고 기동을 거부한다."""
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
