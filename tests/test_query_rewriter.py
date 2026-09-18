from shared.models import SearchHit, SearchSource
from shared.query_rewriter import safe_rewrite, should_rewrite_query
from shared.search_fusion import rrf_merge_hit_lists


def test_safe_rewrite_preserves_numbers_and_sensitive_conditions():
    original = "학사규정이 아닌 일반 대학규정을 7월에서 언제로 앞당겼나요?"
    assert safe_rewrite(original, "일반 대학규정 제개정 7월 앞당긴 일정") is None
    assert safe_rewrite(original, "학사규정이 아닌 일반 대학규정 7월 앞당긴 일정")
    assert safe_rewrite("3월 진로상담 건수", "2024년 3월 진로상담 건수") is None
    assert safe_rewrite("우리 대학 핵심역량", "OO대학교 핵심역량") is None


def test_short_standalone_noun_queries_bypass_rewrite_but_context_and_questions_do_not():
    assert not should_rewrite_query("2026학년도 학사일정")
    assert not should_rewrite_query("선후배 멘토링 신청 서식")
    assert should_rewrite_query("그거 신청 기한은 언제야?")
    assert should_rewrite_query("현장실습 신청은 언제까지인가요?")


def test_rrf_merge_rewards_evidence_retrieved_by_both_queries():
    a = SearchHit("a", 0.1, SearchSource("a"))
    b = SearchHit("b", 0.2, SearchSource("b"))
    c = SearchHit("c", 0.3, SearchSource("c"))
    merged = rrf_merge_hit_lists([[a, b], [c, b]])
    assert [hit.source.file_id for hit in merged] == ["b", "a", "c"]
    weighted = rrf_merge_hit_lists([[a, b], [c, b]], weights=(1, 2))
    assert [hit.source.file_id for hit in weighted] == ["b", "c", "a"]
