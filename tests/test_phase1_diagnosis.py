import json

import scripts.run_phase1_diagnosis as phase1
from scripts.run_phase1_diagnosis import (
    InventoryDocument,
    _diagnosis_label,
    _rag_file_counts,
    audit_status,
    evidence_status,
    lexical_candidates,
    normalized_evidence,
    vector_candidates,
)
from shared.config import Settings


def _doc(file_id: str, *, title: str, bundle: str, body: str) -> InventoryDocument:
    return InventoryDocument(
        file_id=file_id,
        title=title,
        bundle=bundle,
        path=bundle + "/" + title,
        mime_type="application/pdf",
        status="INDEXED",
        indexed=True,
        rag_file_count=1,
        source_object_count=1,
        body=body,
    )


def test_gold_audit_normalizes_amounts_and_dates_before_evidence_match():
    assert "moneywon3000000" in normalized_evidence("300만원")
    assert "moneywon3000000" in normalized_evidence("3,000,000원")
    assert "3000000" in normalized_evidence("3000000")
    assert "date20260315" in normalized_evidence("2026년 3월 15일")
    found, total = evidence_status(
        ["지원 한도 300만원", "신청 기한 2026-03-15"],
        "지원 한도 3,000,000원. 신청 기한 2026년 3월 15일.",
    )
    assert (found, total) == (2, 2)
    assert audit_status(indexed=False, source_count=0, body="", found=0, total=1) == "not_indexed"
    assert (
        audit_status(indexed=True, source_count=0, body="", found=0, total=1)
        == "source_object_missing"
    )
    assert (
        audit_status(indexed=True, source_count=1, body="", found=0, total=1) == "empty_extraction"
    )
    assert (
        audit_status(indexed=True, source_count=1, body="본문", found=0, total=1)
        == "evidence_extraction_failed"
    )


def test_full_inventory_lexical_channels_and_metadata_are_independently_ranked():
    docs = [
        _doc("f1", title="일반 공지", bundle="교무 일반", body="일반 공지 본문"),
        _doc(
            "f2",
            title="2026-2학기 수강신청 안내.pdf",
            bundle="2026학년도 2학기 수강신청",
            body="수강 신청 기간과 절차를 안내합니다.",
        ),
    ]
    query = "2026 2학기 수강신청"
    assert lexical_candidates(query, docs, "body", 200)[0] == "f2"
    assert lexical_candidates(query, docs, "title", 200)[0] == "f2"
    assert lexical_candidates(query, docs, "bundle", 200)[0] == "f2"
    assert lexical_candidates(query, docs, "metadata", len(docs))[0] == "f2"


def test_q1_to_q4_diagnosis_labels_follow_the_fixed_failure_tree():
    assert _diagnosis_label(False, True, True, True) == "query_side_normalization_or_expansion"
    assert _diagnosis_label(False, False, False, True) == "lexical_analyzer_or_field_mapping"
    assert _diagnosis_label(False, False, False, False) == "ingestion_or_index_pipeline"
    assert _diagnosis_label(True, True, True, True) == "retrievable_from_user_query"


def test_vector_channel_uses_timeout_bound_rest_and_deduplicates_file_ids(monkeypatch):
    calls = []

    class Response:
        @staticmethod
        def read():
            return json.dumps(
                {
                    "contexts": {
                        "contexts": [
                            {"sourceDisplayName": "f1.md", "sourceUri": "gs://bucket/f1.md"},
                            {"sourceDisplayName": "f1.md", "sourceUri": "gs://bucket/f1.md"},
                            {"sourceDisplayName": "f2.md", "sourceUri": "gs://bucket/f2.md"},
                        ]
                    }
                }
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(phase1.urllib.request, "urlopen", urlopen)

    settings = Settings(
        gcp_project_id="project",
        gcp_region="asia-northeast3",
        rag_corpus_name="projects/project/locations/asia-northeast3/ragCorpora/corpus",
    )
    ids, status, _latency = vector_candidates("token", settings, "query", 100)
    assert ids == ["f1", "f2"]
    assert status == "ok"
    assert calls[0][1] == 60
    assert json.loads(calls[0][0].data)["query"]["ragRetrievalConfig"]["topK"] == 100


def test_rag_file_inventory_uses_timeout_bound_rest_pagination(monkeypatch):
    calls = []

    class Response:
        @staticmethod
        def read():
            return json.dumps(
                {
                    "ragFiles": [
                        {
                            "displayName": "f1.md",
                            "gcsSource": {"uris": ["gs://bucket/f1.md"]},
                        }
                    ]
                }
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(phase1.urllib.request, "urlopen", urlopen)
    settings = Settings(
        gcp_project_id="project",
        gcp_region="asia-northeast3",
        rag_corpus_name="projects/project/locations/asia-northeast3/ragCorpora/corpus",
    )

    assert _rag_file_counts("token", settings) == {"f1": 1}
    assert calls[0][1] == 60
    assert calls[0][0].full_url.endswith("/ragFiles?pageSize=100")
