import pytest

from shared.lexical_rerank import (
    Bm25Index,
    TitleBm25Index,
    bm25_scores,
    normalize_title,
    rrf_rerank,
    title_scores,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("2026-1학기 수강신청", "2026학년도 1학기 수강신청.pdf"),
        ("연구_윤리--규정", "연구 윤리 규정.hwp"),
        ("운영계획", "운영계획_20260912.pdf"),
        ("운영계획", "운영계획 (2026-09-12 153000).hwpx"),
    ],
)
def test_normalize_title_equivalent_forms(left, right):
    assert normalize_title(left) == normalize_title(right)


def test_title_channel_is_opt_in_and_weighted_for_abcd_canary():
    query = "2026학년도 1학기 수강신청"
    bodies = ["수강신청 안내 본문", "수강신청 안내 본문"]
    titles = ["일반 학사 안내.hwp", "2026-1학기 수강신청.pdf"]

    # A: current, B/C/D: title rank weight 1/2/3. The default remains A.
    assert rrf_rerank(query, bodies, titles=titles) == [0, 1]
    assert rrf_rerank(query, bodies, titles=titles, title_weight=1) == [0, 1]
    assert rrf_rerank(query, bodies, titles=titles, title_weight=2) == [0, 1]
    assert rrf_rerank(query, bodies, titles=titles, title_weight=3) == [1, 0]


def test_title_rerank_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="same length"):
        rrf_rerank("q", ["a", "b"], titles=["a"], title_weight=1)
    with pytest.raises(ValueError, match="nonnegative"):
        rrf_rerank("q", ["a", "b"], titles=["a", "b"], title_weight=-1)


def test_reusable_bm25_indexes_preserve_one_shot_scores():
    documents = ["2026학년도 1학기 수강신청 기간", "장학금 신청 안내", "수강신청 변경"]
    titles = ["2026-1학기 수강신청.pdf", "장학금 안내.hwp", "수강신청 변경 안내.pdf"]
    query = "2026학년도 1학기 수강신청"

    assert Bm25Index(documents).scores(query) == bm25_scores(query, documents)
    assert TitleBm25Index(titles).scores(query) == title_scores(query, titles)
