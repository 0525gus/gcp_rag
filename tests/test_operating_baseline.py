import json

import pytest

import scripts.measure_operating_baseline as baseline


def _golden(case_n: int, tags: list[str]) -> dict:
    return {
        "n": case_n,
        "query": f"query-{case_n}",
        "expected_file": [f"f{case_n}"],
        "also_accept": [],
        "expected_evidence": [f"evidence-{case_n}"],
        "weakness_tags": tags,
    }


def _document(file_id: str, text: str) -> dict:
    return {"source": {"fileId": file_id}, "chunks": [{"text": text}]}


def test_operating_baseline_records_rank_at_15_without_fabricating_candidate_depth(monkeypatch):
    operational_calls: list[int] = []

    def fake_request(_url, _key, query, top_k, _retries):
        operational_calls.append(top_k)
        case_n = int(query.rsplit("-", 1)[1])
        documents = [_document(f"wrong-{index}", "x") for index in range(14)]
        documents.append(_document(f"f{case_n}", f"evidence-{case_n}"))
        documents.extend(_document("outside-depth", "x") for _index in range(5))
        return documents, 12.0, ""

    monkeypatch.setattr(baseline, "_request", fake_request)
    rows, top30_exposed = baseline.run(
        [_golden(1, ["period_conflict"])],
        url="https://mcp.example/mcp",
        key="secret",
        retries=1,
        common={},
    )

    assert operational_calls == [20]
    assert rows[0]["rank_at_15"] == 15
    assert rows[0]["operational_returned_documents"] == 15
    assert rows[0]["candidate_20_rank"] is None
    assert rows[0]["candidate_30_rank"] is None
    assert top30_exposed is False
    assert json.loads(rows[0]["weakness_tags"]) == ["period"]


def test_summary_does_not_fabricate_candidate_recall_at_30_when_capped():
    rows = [
        {
            "weakness_tags": '["numeric_table"]',
            "operational_rank": 1,
            "operational_evidence_found": 1,
            "operational_evidence_total": 1,
            "operational_latency_ms": 15.0,
            "operational_error": "",
            "candidate_20_rank": 1,
            "candidate_20_error": "",
            "candidate_20_depth_exposed": True,
            "candidate_30_rank": 1,
            "candidate_30_error": "",
            "candidate_30_depth_exposed": False,
        }
    ]
    summary = baseline._summary_rows(rows, top30_exposed=False)
    overall = summary[0]

    assert overall["hit@1"] == 1.0
    assert overall["candidate_recall@20"] == 1.0
    assert overall["candidate_recall@30"] is None
    assert overall["candidate_recall@30_status"] == "not_exposed_by_operational_response"


def test_dev150_is_default_and_holdout_sets_require_explicit_step7_guard(monkeypatch):
    monkeypatch.delenv("ALLOW_HOLDOUT_RUN", raising=False)

    assert baseline.DEFAULT_SET == "dev150"
    assert baseline.resolve_target_set("dev150") == "dev"
    with pytest.raises(RuntimeError, match="STEP 7"):
        baseline.resolve_target_set("holdout50")
    with pytest.raises(RuntimeError, match="STEP 7"):
        baseline.resolve_target_set("golden200")

    monkeypatch.setenv("ALLOW_HOLDOUT_RUN", "1")
    assert baseline.resolve_target_set("holdout50") == "holdout"
