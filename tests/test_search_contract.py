"""Public protocol, evidence boundaries and HTML ingestion regression checks."""

import json
import os
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from shared.html_text import clean_html_evidence, html_to_text
from shared.models import DocState, DocStatus, SearchHit, SearchSource
from shared.search_postprocess import CHUNK_JOINER, postprocess_hits
from shared.search_response import response_documents

for key, value in {
    "GCP_PROJECT_ID": "test-project",
    "GCS_HWP_ORIGINAL_BUCKET": "raw",
    "GCS_SOURCE_BUCKET": "norm",
    "RAG_CORPUS_NAME": "projects/p/locations/l/ragCorpora/c",
}.items():
    os.environ.setdefault(key, value)


@pytest.fixture
def server(monkeypatch):
    import services.mcp_server.main as app

    calls = []
    hits = [
        SearchHit("앞부분" + CHUNK_JOINER + "원문에 있는 구분자", 0.1, SearchSource("f1")),
        SearchHit("다른 검색 구간", 0.2, SearchSource("f1")),
        SearchHit("다른 문서 근거", 0.3, SearchSource("f2")),
    ]

    class Rag:
        def retrieve(self, query, **kwargs):
            calls.append((query, kwargs))
            return hits

    class Store:
        def get(self, fid):
            return DocState(
                file_id=fid,
                drive_id="d",
                name=f"{fid}.txt",
                status=DocStatus.INDEXED,
                source_uri=f"https://drive.google.com/file/d/{fid}/view",
            )

    app._cache.clear()
    monkeypatch.setattr(app, "RagEngineClient", lambda *_: Rag())
    monkeypatch.setattr(app, "DocStateStore", lambda *_: Store())
    monkeypatch.setattr(app, "settings", replace(app.settings, search_lexical_rerank=False))
    monkeypatch.setattr(app, "MCP_API_KEY", "test-key")
    yield app, hits, calls
    app._cache.clear()


def test_protocol_exposes_only_search_and_returns_evidence_once(server):
    app, _, calls = server
    headers = {"Authorization": "Bearer test-key", "Accept": "application/json, text/event-stream"}
    with TestClient(app.build_app()) as client:

        def rpc(method, params=None):
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params or {},
                },
            )
            assert response.status_code == 200
            raw = response.text
            if raw.startswith("event:") or raw.startswith("data:"):
                raw = next(
                    line[5:].strip() for line in raw.splitlines() if line.startswith("data:")
                )
            return json.loads(raw)

        listed = rpc("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in listed] == ["search"]
        assert "documents" in listed[0]["outputSchema"]["properties"]
        assert "TOP_K_DEFAULT" in listed[0]["description"]
        assert "7" in listed[0]["description"]
        assert "5" not in listed[0]["description"]
        result = rpc("tools/call", {"name": "search", "arguments": {"query": "규정", "top_k": 5}})[
            "result"
        ]
        payload = result["structuredContent"]
        assert (payload["documentCount"], payload["chunkCount"]) == (2, 3)
        assert payload["schemaVersion"] == 2
        assert len(calls) == 1
        assert [d["citationId"] for d in payload["documents"]] == [1, 2]
        assert len(payload["documents"][0]["chunks"]) == 2
        assert CHUNK_JOINER in payload["documents"][0]["chunks"][0]["text"]
        assert (
            "context" not in payload and "coverage" not in payload and "chunk_count" not in payload
        )
        assert response_documents(result) == payload["documents"]
        # SDK emits a text representation for MCP clients without structured-output support.
        assert json.loads(result["content"][0]["text"]) == payload
        removed = rpc("tools/call", {"name": "answer", "arguments": {"query": "규정"}})
        assert removed.get("error") or removed["result"].get("isError")
        assert len(calls) == 1


def test_empty_search_and_cached_response_do_not_claim_answerability(server):
    app, hits, calls = server
    hits.clear()
    response = app.search("없는 문서")
    assert response == {
        "schemaVersion": 2,
        "documents": [],
        "documentCount": 0,
        "chunkCount": 0,
        "retrievalDiagnostics": None,
    }
    response["documents"].append({"unexpected": True})
    assert app.search("없는 문서")["documents"] == []
    assert len(calls) == 1


def test_new_query_runs_again_but_same_query_uses_cache(server):
    app, _, calls = server
    first = app.search("첫 검색")
    assert app.search("첫 검색") == first
    app.search("새 정보")
    assert len(calls) == 2


def test_search_fuses_raw_and_validated_rewrite_and_falls_back_to_raw(server, monkeypatch):
    app, _, calls = server
    monkeypatch.setattr(app, "settings", replace(app.settings, search_rewrite_enabled=True))
    monkeypatch.setattr(app, "rewrite_query", lambda *_: "독립형 검색 질의")
    original = "원문 질문은 언제까지인가요?"
    app.search(original)
    assert {query for query, _ in calls} == {original, "독립형 검색 질의"}
    assert len(calls) == 2

    app._cache.clear()
    calls.clear()
    monkeypatch.setattr(app, "rewrite_query", lambda *_: None)
    app.search(original)
    assert [query for query, _ in calls] == [original]


def test_short_standalone_query_bypasses_rewriter(server, monkeypatch):
    app, _, calls = server
    monkeypatch.setattr(app, "settings", replace(app.settings, search_rewrite_enabled=True))
    called = []
    monkeypatch.setattr(app, "rewrite_query", lambda *_: called.append(True))
    app.search("2026학년도 학사일정")
    assert called == []
    assert [query for query, _ in calls] == ["2026학년도 학사일정"]


def test_search_returns_preview_for_existing_edit_uri(server, monkeypatch):
    app, _, _ = server
    original = "https://docs.google.com/document/d/f1/edit?resourcekey=key"

    class Store:
        def get(self, fid):
            return DocState(
                file_id=fid,
                drive_id="d",
                name="document",
                status=DocStatus.INDEXED,
                source_uri=original,
            )

    monkeypatch.setattr(app, "DocStateStore", lambda *_: Store())
    result = app.search("문서 보기")
    assert result["documents"]
    for document in result["documents"]:
        assert (
            document["source"]["sourceUri"]
            == "https://drive.google.com/file/d/f1/view?resourcekey=key"
        )


def test_html_preserves_scholarship_table_relationships():
    content = """<html><head><style>noise</style></head><body>
    <h2>성적우수장학금</h2><input type="hidden" value="secret">
    <div class="contntMaster">/WEB-INF/layout.jsp</div>
    <div hidden>숨김</div><script>bad()</script>
    <table><tr><th>구분</th><th>등급</th><th>금액</th><th>조건</th></tr>
    <tr><td rowspan="2">성적우수</td><td>A</td><td>350만원</td>
    <td rowspan="2">2025학번 이후 15학점<br>평점 3.5 이상</td></tr>
    <tr><td>B</td><td>250만원</td></tr></table>
    <p>전공 12학점 이상 &amp; F학점 없음</p>
    <a href="https://example.org/rules">선정기준</a></body></html>"""
    result = html_to_text(content)
    assert "#" in result and "성적우수장학금" in result
    assert "| 성적우수 | A | 350만원 | 2025학번 이후 15학점 평점 3.5 이상 |" in result
    assert "| 성적우수 | B | 250만원 | 2025학번 이후 15학점 평점 3.5 이상 |" in result
    assert "전공 12학점 이상 & F학점 없음" in result
    assert "[선정기준](https://example.org/rules)" in result
    assert all(
        noise not in result for noise in ("<", "secret", "WEB-INF", "bad()", "숨김", "noise")
    )


def test_html_partial_and_escaped_chunks_do_not_lose_visible_text():
    assert "5이상" in clean_html_evidence(
        '5이상&lt;br/&gt;&lt;span style="color:red"&gt;전공 12학점&lt;/span&gt;'
    )
    result = html_to_text("<table><tr><td>A<td>350만원<tr><td>B<td>250만원</table>")
    assert "| A | 350만원 |" in result and "| B | 250만원 |" in result
    plain = "# 학점\n15학점 이상, 3 < 5, 예: <T>"
    assert clean_html_evidence(plain) == plain


def test_long_spanned_conditions_are_kept_once_with_explicit_cell_references():
    condition = "취득학점 15학점 이상, 전공 12학점 이상. " * 8
    result = html_to_text(
        f'<table><tr><td>A</td><td rowspan="2">{condition}</td></tr><tr><td>B</td></tr></table>'
    )
    assert "| A | [공통셀1] |" in result and "| B | [공통셀1] |" in result
    assert "이 표의 공통셀1: " + condition.strip() in result
    assert result.count(condition.strip()) == 1


def test_legacy_html_is_cleaned_but_code_examples_in_other_documents_are_preserved(
    server, monkeypatch
):
    app, hits, _ = server
    hits[:] = [
        SearchHit("<h2>장학금</h2><p>15학점</p>", 0.1, SearchSource("f1")),
        SearchHit("HTML 예제: <div>code</div>", 0.2, SearchSource("f2")),
    ]

    class Store:
        def get(self, fid):
            return DocState(
                file_id=fid,
                drive_id="d",
                name="content.html" if fid == "f1" else "code.txt",
                status=DocStatus.INDEXED,
            )

    monkeypatch.setattr(app, "DocStateStore", lambda *_: Store())
    documents = app.search("장학금")["documents"]
    assert documents[0]["chunks"][0]["text"] == "## 장학금\n\n15학점"
    assert documents[1]["chunks"][0]["text"] == hits[1].text


def test_html_is_cleaned_before_ingestion_hash_and_upload():
    from tests.test_size_and_scale import _GateStore, _GateGcs, _GateDrive, _Settings
    import services.sync.main as sync

    uploaded = []

    class Gcs(_GateGcs):
        def upload_source_md(self, md, fid):
            uploaded.append(md)
            return super().upload_source_md(md, fid)

    body = sync.IngestBody(fileId="f1", driveId="d", name="content.html", mimeType="text/html")
    result = sync._ingest_direct(
        body, _GateStore(), Gcs(), _GateDrive("<h2>장학금</h2><p>15학점</p>".encode()), _Settings()
    )
    assert result["status"] == "GCS_READY"
    assert "15학점" in uploaded[0] and "<h2>" not in uploaded[0]


def test_refresh_content_rebuilds_unchanged_html_preserving_modified_time(monkeypatch):
    from tests.test_recovery_fidelity import _Drive, _Gcs, _LinkStore, _Settings
    import services.sync.main as sync

    modified = "2026-09-09T10:20:45Z"
    existing = DocState(
        file_id="f1",
        drive_id="d",
        name="content.html",
        mime_type="text/html",
        status=DocStatus.INDEXED,
        modified_time=modified,
        path="Drive/content.html",
    )
    store = _LinkStore(existing)
    monkeypatch.setattr(store, "should_reparse", lambda *_: False)
    monkeypatch.setattr(store, "get", lambda *_: existing)

    class Drive(_Drive):
        def download_file(self, fid):
            return b"<h2>New conversion</h2><p>Evidence</p>"

    body = sync.IngestBody(
        fileId="f1", driveId="d", name="content.html", mimeType="text/html", modifiedTime=modified
    )
    assert (
        sync._ingest_with(body, store=store, gcs=_Gcs(), drive=Drive(), settings=_Settings())[
            "status"
        ]
        == "UNCHANGED"
    )
    refreshed = body.model_copy(update={"refresh_content": True})
    result = sync._ingest_with(
        refreshed, store=store, gcs=_Gcs(), drive=Drive(), settings=_Settings()
    )
    assert result["status"] == "GCS_READY"
    assert store.saved[-1].modified_time == modified


def test_postprocessing_records_original_chunks_and_scores():
    hits = [
        SearchHit("a" + CHUNK_JOINER + "b", 0.1, SearchSource("f1")),
        SearchHit("c", 0.3, SearchSource("f1")),
    ]
    result = postprocess_hits(hits, top_k=1)[0]
    assert [c.text for c in result.chunks] == [h.text for h in hits]
    assert [c.score for c in result.chunks] == [0.1, 0.3]


def test_evaluation_reads_legacy_results():
    docs = [{"text": "본문", "source": {"fileId": "f1"}, "score": 0.1}]
    assert response_documents({"content": [{"type": "text", "text": json.dumps(docs[0])}]}) == docs
    with pytest.raises(ValueError, match="tool error"):
        response_documents({"isError": True})
