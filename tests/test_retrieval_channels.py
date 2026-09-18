import csv
import json
import os
from dataclasses import replace

from starlette.testclient import TestClient

from scripts.eval_retrieval_channels import (
    DEFAULT_SNAPSHOT,
    add_channel_columns,
    load_snapshot,
    write_flat_tables,
)
from shared.models import DocState, DocStatus, SearchHit, SearchSource
from shared.retrieval_channels import RETRIEVAL_CHANNELS, rank_channel


def test_individual_channel_rankings_preserve_one_vector_candidate_pool():
    values = {
        "bodies": ["일반 공지", "수강 신청 기간과 절차"],
        "titles": ["일반 공지.hwp", "2026-2학기 수강신청.pdf"],
        "bundles": ["교무 일반", "2026학년도 2학기 수강신청"],
        "metadata": ["2026 1학기 일반 공지", "2026 2학기 수강신청 신청서"],
    }
    assert RETRIEVAL_CHANNELS == ("vector", "body", "title", "bundle", "metadata")
    assert rank_channel("2026 2학기 수강신청", "vector", **values).order == [0, 1]
    for channel in ("body", "title", "bundle", "metadata"):
        ranking = rank_channel("2026 2학기 수강신청", channel, **values)
        assert ranking.order[0] == 1
    metadata = rank_channel("2026 2학기 수강신청", "metadata", **values)
    assert metadata.signals is not None
    assert metadata.signals[0].period < 0
    assert metadata.signals[1].period > 0


def test_authenticated_http_paths_expose_each_channel(monkeypatch):
    for key, value in {
        "GCP_PROJECT_ID": "test-project",
        "GCS_HWP_ORIGINAL_BUCKET": "raw",
        "GCS_SOURCE_BUCKET": "norm",
        "RAG_CORPUS_NAME": "projects/p/locations/l/ragCorpora/c",
    }.items():
        os.environ.setdefault(key, value)
    import services.mcp_server.main as app

    hits = [
        SearchHit("일반 공지", 0.1, SearchSource("f1")),
        SearchHit("수강 신청 기간과 절차", 0.2, SearchSource("f2")),
    ]

    class Rag:
        def retrieve(self, query, **kwargs):
            return hits

    class Store:
        def get(self, file_id):
            return DocState(
                file_id=file_id,
                drive_id="drive",
                name="일반 공지.hwp" if file_id == "f1" else "2026-2학기 수강신청.pdf",
                bundle="교무 일반" if file_id == "f1" else "2026학년도 2학기 수강신청",
                status=DocStatus.INDEXED,
            )

    monkeypatch.setattr(app, "RagEngineClient", lambda *_: Rag())
    monkeypatch.setattr(app, "DocStateStore", lambda *_: Store())
    monkeypatch.setattr(app, "settings", replace(app.settings, search_fetch_multiplier=1))
    monkeypatch.setattr(app, "ROUTING_MODE", "single")
    monkeypatch.setattr(app, "MCP_API_KEY", "test-key")
    app.mcp._session_manager = None
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app.build_app()) as client:
        assert client.post("/retrieval/vector", json={"query": "q"}).status_code == 401
        for channel in RETRIEVAL_CHANNELS:
            response = client.post(
                f"/retrieval/{channel}",
                headers=headers,
                json={"query": "2026 2학기 수강신청", "top_k": 2, "candidate_k": 2},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["channel"] == channel
            assert payload["candidateSource"] == "vertex_vector"
            assert len(payload["results"]) == 2
        assert (
            client.post("/retrieval/nope", headers=headers, json={"query": "q"}).status_code == 404
        )
        assert (
            client.post(
                "/retrieval/vector",
                headers=headers,
                json={"query": "q", "top_k": 30, "candidate_k": 20},
            ).status_code
            == 400
        )
    app.mcp._session_manager = None


def test_frozen_split_and_flat_table_are_one_row_per_query(tmp_path):
    manifest, golden = load_snapshot(DEFAULT_SNAPSHOT, "dev")
    assert manifest["snapshot_id"] == "golden200-2026-09-13"
    assert len(golden) == 150

    row = {
        "snapshot_id": manifest["snapshot_id"],
        "commit": "abc123",
        "case_n": golden[0]["n"],
        "query": golden[0]["query"],
    }
    payload = {
        "candidateCount": 1,
        "scoreType": "vector_rank_reciprocal",
        "serviceRevision": "rag-mcp-test",
        "commit": "server123",
        "latencyMs": {"total": 4.0, "retrieve": 3.0, "rank": 0.1},
        "results": [
            {
                "source": {
                    "fileId": golden[0]["expected_file"][0],
                    "name": golden[0].get("name", ""),
                    "bundle": golden[0]["expected_bundle"][0],
                },
                "chunks": [{"text": " ".join(map(str, golden[0]["expected_evidence"]))}],
            }
        ],
    }
    add_channel_columns(row, "vector", payload, golden[0], 5.0)
    csv_path, jsonl_path = write_flat_tables(tmp_path, [row])

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    jsonl_rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert len(csv_rows) == len(jsonl_rows) == 1
    assert csv_rows[0]["snapshot_id"] == manifest["snapshot_id"]
    assert jsonl_rows[0]["commit"] == "abc123"
    assert jsonl_rows[0]["vector_rank"] == 1
