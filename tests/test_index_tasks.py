from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import services.sync.main as sync_main
from shared.rag_engine import ImportOutcome
from shared.task_queue import IndexTaskQueue
from shared.models import DocState, DocStatus
from shared.firestore_state import DocStateStore
from shared.index_guard import document_version, routing_version
from fake_firestore import MemoryDb


FILE_ID = "1eetIrUEnpmrFEn9ui2fQ6Yfr2piiBKrv"


class FakeSnap:
    def __init__(self, data=None) -> None:
        self.data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self.data or {})


class FakeRef:
    def __init__(self, data=None) -> None:
        self.data = data
        self.children: dict[str, FakeRef] = {}

    def get(self):
        return FakeSnap(self.data)

    def set(self, payload, merge=False) -> None:
        self.data = {**(self.data or {}), **payload} if merge else dict(payload)

    def collection(self, _name: str):
        return self

    def document(self, name: str):
        return self.children.setdefault(name, FakeRef())


class FakeBatch:
    def __init__(self) -> None:
        self.writes = []

    def set(self, ref, payload, **_kwargs) -> None:
        self.writes.append((ref, payload))

    def commit(self) -> None:
        for ref, payload in self.writes:
            ref.set(payload)


def _settings(**overrides):
    base = {
        "cloud_tasks_enabled": True,
        "gcp_project_id": "p",
        "task_queue_location": "r",
        "task_queue_faculty": "faculty-q",
        "task_queue_student": "student-q",
        "task_service_account": "worker@p.iam.gserviceaccount.com",
        "sync_task_base_url": "https://sync.example",
        "index_job_timeout_seconds": 900,
        "audience_split_enabled": True,
        "departments": (),
        "sync_job_collection": "sync_jobs",
        "gcs_source_bucket": "source",
        "gcs_hwp_original_bucket": "raw",
        "rag_corpus_name": "corpus/staff",
        "rag_corpus_name_student": "corpus/student",
        "student_folder_ids": "student-folder",
        "sync_folder_ids": "folder",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_task_queue_builds_json_oidc_request() -> None:
    captured = {}

    class FakeClient:
        def queue_path(self, *parts):
            return "/".join(parts)

        def task_path(self, *parts):
            return "/".join(parts)

        def create_task(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(name=kwargs["task"].name)

    producer = object.__new__(IndexTaskQueue)
    producer.settings = _settings()
    producer._client = FakeClient()
    name = producer.enqueue(queue="faculty-q", task_id="job-faculty", payload={"jobId": "job"})

    task = captured["task"]
    assert name.endswith("faculty-q/job-faculty")
    assert task.http_request.url == "https://sync.example/sync/index-gcs-task"
    assert task.http_request.oidc_token.audience == "https://sync.example"
    assert json.loads(task.http_request.body) == {"jobId": "job"}
    assert task.dispatch_deadline.seconds == 900


def test_async_index_enqueues_independent_faculty_student_tasks(monkeypatch) -> None:
    settings = _settings()
    job_ref = FakeRef()
    parts_ref = FakeRef()
    fake_store = SimpleNamespace(_db=SimpleNamespace(batch=lambda: FakeBatch()))
    enqueued = []

    class FakeStateStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get(self, _file_id):
            return DocState(
                file_id=_file_id,
                drive_id="drive",
                status=DocStatus.PARSED,
                content_hash="revision-1",
            )

    class FakeProducer:
        def __init__(self, _settings) -> None:
            pass

        def enqueue(self, **kwargs):
            enqueued.append(kwargs)
            return kwargs["task_id"]

    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStateStore)
    monkeypatch.setattr(
        sync_main,
        "_index_job_refs",
        lambda _settings, _job: (fake_store, job_ref, parts_ref),
    )
    monkeypatch.setattr(sync_main, "IndexTaskQueue", FakeProducer)

    result = sync_main.index_gcs_async(
        sync_main.IndexGcsBody(
            driveId="drive",
            gcsUris=[f"gs://source/{FILE_ID}.md"],
            fileIds=[FILE_ID],
        )
    )

    assert result["parts"] == ["faculty", "student"]
    assert [item["queue"] for item in enqueued] == ["faculty-q", "student-q"]
    assert {item["payload"]["audience"] for item in enqueued} == {
        "FACULTY",
        "STUDENT",
    }
    assert enqueued[0]["payload"]["fileVersions"][FILE_ID]
    assert enqueued[0]["payload"]["routingVersion"] == routing_version(settings)


def setup_job(monkeypatch, *, job_id="job", parts=("faculty",), db=None):
    settings = _settings(audience_split_enabled="student" in parts)
    store = object.__new__(DocStateStore)
    store.settings = settings
    store._db = db or MemoryDb()
    store._col = store._db.collection("doc_state")
    store._tokens = store._db.collection("sync_tokens")
    state = DocState(
        file_id=FILE_ID,
        drive_id="drive",
        status=DocStatus.PARSED,
        content_hash="v1",
        modified_time="2026-09-01T00:00:00Z",
    )
    if not store._col.document(FILE_ID).get().exists:
        store.upsert(state)
    versions = {FILE_ID: document_version(store.get(FILE_ID))}
    job = store._db.collection("sync_jobs").document(job_id)
    job.set(
        {
            "status": "RUNNING",
            "driveId": "drive",
            "fileIds": [FILE_ID],
            "gcsUris": [f"gs://source/{FILE_ID}.md"],
            "fileVersions": versions,
            "routingVersion": routing_version(settings),
            "expectedParts": list(parts),
        }
    )
    for pid in parts:
        job.collection("parts").document(pid).set({"status": "QUEUED", "audience": pid.upper()})
    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        sync_main,
        "_index_job_refs",
        lambda _s, jid: (
            store,
            store._db.collection("sync_jobs").document(jid),
            store._db.collection("sync_jobs").document(jid).collection("parts"),
        ),
    )
    monkeypatch.setattr(sync_main, "RagEngineClient", lambda *_a, **_k: object())
    body = sync_main.IndexGcsTaskBody(
        jobId=job_id,
        partId="faculty",
        driveId="drive",
        audience="FACULTY",
        gcsUris=[f"gs://source/{FILE_ID}.md"],
        fileIds=[FILE_ID],
        fileVersions=versions,
        routingVersion=routing_version(settings),
    )
    return store, job, body


def test_task_retry_is_idempotent_after_part_done(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda *_a, **_kw: pytest.fail("duplicate import")
    )
    result = sync_main.index_gcs_task(body)
    assert result["status"] == "DONE"
    assert result["idempotent"] is True
    assert store.get(FILE_ID).status == DocStatus.INDEXED
    assert job.get().to_dict()["status"] == "DONE"


def test_task_partial_failure_stays_retryable(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda *_a, **_kw: ImportOutcome(body.gcs_uris, imported=0, failed=1, skipped=0),
    )
    with pytest.raises(RuntimeError, match="faculty import incomplete"):
        sync_main.index_gcs_task(body)
    assert job.collection("parts").document("faculty").get().to_dict()["status"] == "RETRYING"
    assert not store._tokens.document(f"__mutation__{FILE_ID}").get().exists
    assert store.get(FILE_ID).status == DocStatus.PARSED
