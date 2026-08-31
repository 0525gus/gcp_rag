from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import services.sync.main as sync_main
from shared.rag_engine import ImportOutcome
from shared.task_queue import IndexTaskQueue


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
    name = producer.enqueue(
        queue="faculty-q", task_id="job-faculty", payload={"jobId": "job"}
    )

    task = captured["task"]
    assert name.endswith("faculty-q/job-faculty")
    assert task.http_request.url == "https://sync.example/sync/index-gcs-task"
    assert task.http_request.oidc_token.audience == "https://sync.example"
    assert json.loads(task.http_request.body) == {"jobId": "job"}


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
            return SimpleNamespace(drive_id="drive")

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


def test_task_retry_is_idempotent_after_part_done(monkeypatch) -> None:
    settings = _settings()
    part = FakeRef({"status": "DONE"})
    parts = FakeRef()
    parts.children["faculty"] = part
    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        sync_main,
        "_index_job_refs",
        lambda _settings, _job: (SimpleNamespace(), FakeRef({}), parts),
    )
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DONE task must not import again")
        ),
    )

    result = sync_main.index_gcs_task(
        sync_main.IndexGcsTaskBody(
            jobId="job",
            partId="faculty",
            driveId="drive",
            audience="FACULTY",
            gcsUris=[f"gs://source/{FILE_ID}.md"],
            fileIds=[FILE_ID],
        )
    )
    assert result == {"status": "DONE", "idempotent": True}


def test_task_partial_failure_stays_retryable(monkeypatch) -> None:
    settings = _settings(audience_split_enabled=False)
    part = FakeRef({"status": "QUEUED"})
    parts = FakeRef()
    parts.children["faculty"] = part
    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        sync_main,
        "_index_job_refs",
        lambda _settings, _job: (SimpleNamespace(), FakeRef({}), parts),
    )
    monkeypatch.setattr(sync_main, "RagEngineClient", lambda *_a, **_k: object())
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda *_a, **_k: ImportOutcome(
            [f"gs://source/{FILE_ID}.md"], imported=0, failed=1, skipped=0
        ),
    )

    with pytest.raises(RuntimeError, match="faculty import incomplete"):
        sync_main.index_gcs_task(
            sync_main.IndexGcsTaskBody(
                jobId="job",
                partId="faculty",
                driveId="drive",
                audience="FACULTY",
                gcsUris=[f"gs://source/{FILE_ID}.md"],
                fileIds=[FILE_ID],
            )
        )
    assert part.data["status"] == "RETRYING"
