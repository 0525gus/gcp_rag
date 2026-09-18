from shared.lexical_rerank import rrf_rerank
from shared.temporal_rank import (
    PeriodMetadata,
    extract_period_metadata,
    temporal_compatibility,
)


def test_extracts_supported_korean_period_metadata():
    result = extract_period_metadata(
        "2026학년도 2학기 하반기 9월 2차 제6회 모집"
    )

    assert result == PeriodMetadata(
        years=frozenset({2026}),
        year_kinds=frozenset({"학년도"}),
        semesters=frozenset({2}),
        halves=frozenset({2}),
        months=frozenset({9}),
        rounds=frozenset({2}),
        editions=frozenset({6}),
    )


def test_year_spellings_and_bare_year_are_extracted_consistently():
    assert extract_period_metadata("2026년").years == frozenset({2026})
    assert extract_period_metadata("2026년도").years == frozenset({2026})
    assert extract_period_metadata("2026 모집").years == frozenset({2026})


def test_match_boost_missing_is_neutral_and_explicit_conflict_is_strong():
    query = extract_period_metadata("2026 하반기")

    assert temporal_compatibility(query, extract_period_metadata("2026 하반기 안내")) == 2
    assert temporal_compatibility(query, extract_period_metadata("모집 안내")) == 0
    assert temporal_compatibility(query, extract_period_metadata("2026 상반기 안내")) == -2
    assert temporal_compatibility(query, extract_period_metadata("2025 상반기 안내")) == -6


def test_multiple_document_values_match_when_any_expected_value_overlaps():
    query = extract_period_metadata("2026학년도 1학기")
    document = extract_period_metadata("2025~2026학년도 1, 2학기 일정")

    assert temporal_compatibility(query, document) == 2


def test_temporal_rule_promotes_match_and_penalizes_half_year_conflict():
    query = "2026 하반기 강사 채용"
    bodies = ["강사 채용", "강사 채용", "강사 채용"]
    metadata = ["2026 상반기 강사 채용", "2026 하반기 강사 채용", "강사 채용"]

    assert rrf_rerank(query, bodies, temporal_metadata=metadata) == [0, 1, 2]
    assert rrf_rerank(
        query, bodies, temporal_metadata=metadata, temporal_weight=0.1
    ) == [1, 2, 0]


def test_dates_extract_month_but_unrelated_numbers_do_not_create_round():
    result = extract_period_metadata("2026년 9월 2일, 1차원 배열과 2차선 도로")

    assert result.months == frozenset({9})
    assert result.rounds == frozenset()
