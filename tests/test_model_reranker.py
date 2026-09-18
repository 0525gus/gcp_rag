import json

from shared.model_reranker import RerankRecord, cross_encoder_order, llm_reranker_order


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _Session:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.body)


def _records():
    return [
        RerankRecord("0", "상반기", "/a", "묶음", "2026", "본문 A"),
        RerankRecord("1", "하반기", "/b", "묶음", "2026", "본문 B"),
    ]


def test_cross_encoder_sends_all_fields_and_uses_returned_order():
    session = _Session({"records": [{"id": "1"}, {"id": "0"}]})

    order = cross_encoder_order("2026 하반기", _records(), project_id="p", session=session)

    assert order == [1, 0]
    payload = session.calls[0][1]["json"]
    assert payload["topN"] == 2
    assert "path: /a" in payload["records"][0]["content"]
    assert "metadata: 2026" in payload["records"][0]["content"]


def test_llm_reranker_requires_a_complete_permutation():
    valid = _Session({
        "candidates": [{"content": {"parts": [{"text": json.dumps(
            {"ordered_ids": ["1", "0"]}
        )}]}}]
    })
    invalid = _Session({
        "candidates": [{"content": {"parts": [{"text": json.dumps(
            {"ordered_ids": ["1", "1"]}
        )}]}}]
    })

    assert llm_reranker_order("q", _records(), project_id="p", session=valid) == [1, 0]
    assert llm_reranker_order("q", _records(), project_id="p", session=invalid) is None


def test_partial_model_order_appends_missing_candidates_in_rrf_order():
    session = _Session({
        "candidates": [{"content": {"parts": [{"text": json.dumps(
            {"ordered_ids": ["1"]}
        )}]}}]
    })

    assert llm_reranker_order("q", _records(), project_id="p", session=session) == [1, 0]


def test_llm_reranker_reports_usage_metadata():
    session = _Session({
        "usageMetadata": {
            "promptTokenCount": 123,
            "candidatesTokenCount": 7,
            "totalTokenCount": 130,
        },
        "candidates": [{"content": {"parts": [{"text": json.dumps(
            {"ordered_ids": ["0", "1"]}
        )}]}}],
    })
    metrics = {}

    llm_reranker_order("q", _records(), project_id="p", session=session, metrics=metrics)

    assert metrics == {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130}


def test_empty_candidates_do_not_call_external_service():
    session = _Session({})

    assert cross_encoder_order("q", [], project_id="p", session=session) == []
    assert llm_reranker_order("q", [], project_id="p", session=session) == []
    assert session.calls == []
