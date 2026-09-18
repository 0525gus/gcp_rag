import json
from pathlib import Path

import pytest

from scripts.eval_golden import (
    evidence_recall,
    latency_summary,
    normalize_evidence_text,
    normalize_golden,
    score,
)


def test_normalize_golden_supports_new_and_legacy_schema():
    current = normalize_golden(
        {
            "query": "질문",
            "expected_file": ["f1", "f2"],
            "expected_bundle": "묶음",
            "expected_evidence": ["정답 문장"],
        },
        1,
    )
    legacy = normalize_golden({"q": "질문", "file_id": "f1", "bundle": "묶음"}, 2)

    assert current["expected_file"] == ["f1", "f2"]
    assert current["expected_bundle"] == ["묶음"]
    assert legacy["query"] == "질문"
    assert legacy["expected_evidence"] == []


def test_normalize_golden_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="expected_file"):
        normalize_golden({"query": "질문"}, 7)


def test_evidence_recall_supports_whitespace_case_and_alternatives():
    docs = [{"chunks": [{"text": "신청 기간은  9월 12일까지입니다. ABC"}]}]
    found, total = evidence_recall(
        ["신청 기간은 9월 12일까지", ["틀린 표현", "abc"]], docs
    )

    assert (found, total) == (2, 2)


def test_evidence_recall_ignores_markdown_table_and_heading_layout():
    docs = [{"chunks": [{"text": "제목.hwp\n교수,3명,1명"}]}]

    found, total = evidence_recall(
        ["# 제목.hwp", "| 교수 | 3명 | 1명 |"], docs
    )

    assert (found, total) == (2, 2)


def test_evidence_normalization_handles_width_html_date_and_money_forms():
    assert normalize_evidence_text("Ａ&amp;Ｂ") == normalize_evidence_text("A&B")
    assert normalize_evidence_text("2026-03-15") == normalize_evidence_text("2026년 3월 15일")
    assert normalize_evidence_text("300만원") == normalize_evidence_text("3,000,000원")


def test_evidence_normalization_does_not_join_adjacent_numeric_table_cells():
    normalized = normalize_evidence_text("지역연계형,22-1,2021122029,이동빈")

    assert normalized == "지역연계형 22 1 2021122029 이동빈"
    assert normalize_evidence_text("1,331명") == "1331명"


def test_date_normalization_does_not_consume_next_table_row():
    normalized = normalize_evidence_text("2027년 2월\n2차년도")

    assert normalized == "2027년 2월 2차년도"


def test_score_exposes_all_requested_hit_rates():
    result = score([{"rank": 1}, {"rank": 3}, {"rank": None}], "rank")

    assert result["hit@1_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert result["hit@3_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert result["hit@5_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert result["mrr"] == 0.444


def test_latency_summary_uses_nearest_rank_p95():
    result = latency_summary([10, 20, 30, 40, 50])

    assert result == {"mean_ms": 30.0, "median_ms": 30, "p95_ms": 50, "max_ms": 50}


def test_golden100_has_complete_e2e_labels():
    rows = json.loads(
        (Path(__file__).parent / "golden100.json").read_text(encoding="utf-8")
    )

    assert len(rows) == 100
    assert [row["n"] for row in rows] == list(range(1, 101))
    for row in rows:
        assert row["query"]
        assert row["expected_file"]
        assert row["expected_bundle"]
        assert row["expected_answer"]
        assert row["must_include"]
        assert isinstance(row["must_not_include"], list)
        assert 1 <= len(row["expected_evidence"]) <= 5
        assert "label_audit" in row


def test_golden200_adds_100_weakness_focused_unique_sources():
    rows = json.loads(
        (Path(__file__).parent / "golden200.json").read_text(encoding="utf-8")
    )

    assert len(rows) == 200
    assert [row["n"] for row in rows] == list(range(1, 201))
    assert len({row["query"] for row in rows}) == 200
    assert len({row["expected_file"] for row in rows}) == 200
    new_rows = rows[100:]
    assert len({row["expected_bundle"] for row in new_rows}) == 100
    assert {row["generation_target"] for row in new_rows} == {
        "period_conflict",
        "sequence_conflict",
        "revision_conflict",
        "numeric_table",
        "similar_title_bundle",
        "multi_condition",
        "distributed_evidence",
    }
    for row in new_rows:
        assert row["query"]
        assert row["expected_answer"]
        assert row["must_include"]
        assert isinstance(row["must_not_include"], list)
        assert 1 <= len(row["expected_evidence"]) <= 5
        assert row["weakness_tags"]
        assert "label_audit" in row
