"""Department isolation, bounded paging and honest batch vs file error reporting."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scripts import dept_gui, index_issues


class Db:
    def __init__(self, rows):
        self.rows = rows

    def collection(self, name):
        return Ref(self, name)

    def get_all(self, references, **kwargs):
        return [ref.get() for ref in references]


class Ref:
    def __init__(self, db, path, filters=(), after="", maximum=10000):
        self.db, self.path = db, path
        self.filters, self.after, self.maximum = filters, after, maximum
        self.id = path.rsplit("/", 1)[-1]

    def document(self, name):
        return Ref(self.db, f"{self.path}/{name}")

    collection = document

    def get(self, **kwargs):
        data = self.db.rows.get(self.path)
        return SimpleNamespace(id=self.id, exists=data is not None, to_dict=lambda: data, reference=self)

    def where(self, *, filter):
        return Ref(self.db, self.path, (*self.filters, filter), self.after, self.maximum)

    def order_by(self, field):
        assert field == "__name__"
        return self

    def start_after(self, values):
        return Ref(self.db, self.path, self.filters, values["__name__"].id, self.maximum)

    def limit(self, maximum):
        return Ref(self.db, self.path, self.filters, self.after, maximum)

    def stream(self, **kwargs):
        result = []
        for path, data in sorted(self.db.rows.items()):
            if path.rsplit("/", 1)[0] != self.path or path.rsplit("/", 1)[-1] <= self.after:
                continue
            if all((data.get(f.field_path) == f.value if f.op_string == "=="
                    else f.value in data.get(f.field_path, [])) for f in self.filters):
                result.append(Ref(self.db, path).get())
        return iter(result[:self.maximum])


NAMES = {"docState": "docs", "dlq": "errors", "splitQueue": "split", "jobs": "jobs"}


def test_paging_scans_healthy_rows_without_claiming_no_more_issues():
    db = Db({"docs/a": {"driveId": "cs", "status": "INDEXED"},
             "docs/b": {"driveId": "cs", "status": "INDEXED"},
             "docs/c": {"driveId": "cs", "status": "PARSED"},
             "docs/d": {"driveId": "ee", "status": "FAILED"},
             "docs/e": {"driveId": "cs2", "status": "FAILED"}})
    first = index_issues.list_issues(db, "docs", ["cs", "cs2"], scan_limit=2)
    assert first["items"] == [] and first["nextCursor"]
    second = index_issues.list_issues(db, "docs", ["cs", "cs2"], scan_limit=2, cursor=first["nextCursor"])
    assert [x["fileId"] for x in second["items"]] == ["c", "e"]
    assert not second["nextCursor"]
    with pytest.raises(ValueError):
        index_issues.list_issues(db, "docs", ["ee"], cursor=first["nextCursor"])


@pytest.mark.parametrize("status,reason", [("EXCLUDED", ""), ("DELETED", ""), ("SKIPPED", "out_of_folder_scope"), ("INDEXED", "")])
def test_resolved_or_out_of_scope_documents_do_not_appear(status, reason):
    assert index_issues.issue_item("abc", {"status": status, "error": reason}) is None


def test_split_queue_and_metadata_only_documents_are_distinct():
    db = Db({"docs/a": {"driveId": "cs", "status": "FAILED", "error": "SIZE_EXCEEDED"},
             "split/a": {"driveId": "cs"},
             "docs/b": {"driveId": "cs", "status": "INDEXED", "error": "NO_BODY_EXTRACTOR:image/png"}})
    result = index_issues.list_issues(db, "docs", ["cs"], split_collection="split")
    assert [x["status"] for x in result["items"]] == ["SPLIT_QUEUED", "BODY_MISSING"]


def test_detail_keeps_batch_failure_separate_from_unconfirmed_document():
    db = Db({"docs/a": {"driveId": "cs", "status": "PARSED"},
             "jobs/j": {"driveId": "cs", "fileIds": ["a", "b"], "kind": "INDEX_GCS",
                        "status": "FAILED", "error": "deadline", "createdAt": "2026-10-06"},
             "jobs/j/parts/faculty": {"status": "RETRYING", "error": "one failed"},
             "jobs/foreign": {"driveId": "ee", "fileIds": ["a"], "kind": "INDEX_GCS"}})
    detail = index_issues.issue_detail(db, NAMES, ["cs"], "a")
    assert detail["item"]["status"] == "PARSED"
    assert detail["jobs"][0]["error"] == "deadline" and len(detail["jobs"]) == 1
    assert "배치" in detail["batchNotice"]
    with pytest.raises(FileNotFoundError):
        index_issues.issue_detail(db, NAMES, ["ee"], "a")


def test_recovered_document_does_not_show_old_dlq_error():
    db = Db({"docs/a": {"driveId": "cs", "status": "INDEXED"}, "errors/a": {"reason": "old"}})
    assert index_issues.issue_detail(db, NAMES, ["cs"], "a")["resolved"]


def test_api_failure_is_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(dept_gui, "_index_issue_context", MagicMock(side_effect=RuntimeError("permission denied")))
    response = TestClient(dept_gui.app).get("/api/v1/departments/cs/index-issues")
    assert response.status_code == 503
    assert "permission denied" in response.json()["error"]["message"]


def test_file_evidence_filters_other_documents_and_departments(monkeypatch):
    uri = "gs://meta-bucket/import-results/123/abcd.ndjson"
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "token")
    monkeypatch.setattr(dept_gui, "_http_post_json", lambda *a, **kw: (200, {"entries": [
        {"textPayload": f"RAG import result sink read sink={uri}", "timestamp": "2026-10-06T00:01:00Z"},
        {"textPayload": "sink=gs://foreign-bucket/import-results/456/abcd.ndjson"},
    ]}, 0))
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'\n'.join([
        b'{"Filename":"gs://source-bucket/file1.docx","Status":"INVALID_ARGUMENT","Message":"File is empty"}',
        b'{"Filename":"gs://source-bucket/file12.docx","Status":"INVALID_ARGUMENT"}',
        b'{"Filename":"gs://foreign-bucket/file1.docx","Status":"INVALID_ARGUMENT"}',
    ])
    get = MagicMock(return_value=response)
    monkeypatch.setattr(dept_gui.urllib.request, "urlopen", get)
    detail = {"jobs": [{"createdAt": "2026-10-06T00:00:00Z", "deadlineAt": "2026-10-06T00:15:00Z"}], "notices": []}
    result = dept_gui._index_issue_evidence("file1", {"buckets": {"source": "source-bucket"}, "corpora": {"staff": "projects/p/locations/r/ragCorpora/123"}},
                                          {"GCP_PROJECT_ID": "p", "RAG_METADATA_BUCKET": "meta-bucket"}, detail)
    assert len(result) == 1 and result[0]["error"] == "File is empty"
    assert get.call_count == 1
