from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from google.api_core.exceptions import DeadlineExceeded
from starlette.exceptions import HTTPException

import services.sync.main as sync_main
from shared.index_guard import MutationLease, StaleIndexTask, document_mutation
from shared.models import DocStatus
from shared.rag_engine import ImportOutcome
from test_index_tasks import FILE_ID, setup_job


def test_concurrent_delivery_performs_rag_mutation_once(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    entered, release = Event(), Event()
    calls = []

    def importing(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return ImportOutcome(body.gcs_uris, 1, 0, 0)

    monkeypatch.setattr(sync_main, "_import_and_mark", importing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sync_main.index_gcs_task, body)
        assert entered.wait(5)
        try:
            with pytest.raises(HTTPException) as error:
                pool.submit(sync_main.index_gcs_task, body).result(timeout=5)
            assert error.value.status_code == 409
            assert (
                job.collection("parts").document("faculty").get().to_dict()["status"] == "RUNNING"
            )
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "DONE"
    assert calls == [1]
    assert store.get(FILE_ID).status == DocStatus.INDEXED


def test_simultaneous_claims_have_one_owner(monkeypatch):
    store, _, _ = setup_job(monkeypatch)
    barrier = Barrier(2)

    def claim():
        lease = MutationLease(store, [FILE_ID])
        barrier.wait(timeout=5)
        try:
            return lease.claim()
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result(timeout=5) for f in futures) == [False, True]


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_hash", "v2"),
        ("modified_time", "2026-09-02T00:00:00Z"),
        ("audience", "STUDENT"),
        ("status", DocStatus.DELETED),
    ],
)
def test_old_revision_never_reaches_rag(monkeypatch, field, value):
    store, job, body = setup_job(monkeypatch)
    state = store.get(FILE_ID)
    setattr(state, field, value)
    store.upsert(state)
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda *_a, **_kw: pytest.fail("stale import")
    )
    assert sync_main.index_gcs_task(body)["status"] == "FAILED"
    assert job.get().to_dict()["status"] == "FAILED"
    assert getattr(store.get(FILE_ID), field) == value


def test_older_job_finishing_after_newer_job_does_not_mark_new_version_indexed(monkeypatch):
    store, old_job, old = setup_job(monkeypatch, job_id="old")
    old_job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    state = store.get(FILE_ID)
    state.content_hash = "v2"
    store.upsert(state)
    _, new_job, new = setup_job(monkeypatch, job_id="new", db=store._db)
    assert sync_main._finalize_index_job(store.settings, "old")["status"] == "FAILED"
    assert store.get(FILE_ID).status == DocStatus.PARSED
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda *_a, **_k: ImportOutcome(new.gcs_uris, 1, 0, 0)
    )
    assert sync_main.index_gcs_task(new)["status"] == "DONE"
    assert new_job.get().to_dict()["status"] == "DONE"


def test_finalizer_compare_and_set_retries_on_concurrent_revision_change(monkeypatch):
    store, job, _ = setup_job(monkeypatch)
    job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    state = store.get(FILE_ID)
    state.content_hash = "new"
    store._db.before_commit = lambda: store.upsert(state)
    result = sync_main._finalize_index_job(store.settings, "job")
    assert result["status"] == "FAILED"
    assert store.get(FILE_ID).status == DocStatus.PARSED
    assert store.get(FILE_ID).content_hash == "new"


def test_unstarted_expired_lease_can_be_taken_over_but_old_owner_is_fenced(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    part = job.collection("parts").document("faculty")
    first = MutationLease(store, [FILE_ID], versions=body.file_versions, job_ref=job, part_ref=part)
    assert first.claim()
    expired = datetime.now(UTC) - timedelta(seconds=1)
    for ref in [*first.refs, part]:
        ref.set({"leaseExpiresAt": expired}, merge=True)
    second = MutationLease(
        store, [FILE_ID], versions=body.file_versions, job_ref=job, part_ref=part
    )
    assert second.claim()
    with pytest.raises(StaleIndexTask):
        first.pin()
    first.finish(error=StaleIndexTask("old owner"))
    assert part.get().to_dict()["owner"] == second.owner
    assert second.refs[0].get().to_dict()["owner"] == second.owner


def test_inflight_mutation_is_not_stolen_after_timeout(monkeypatch):
    store, _, _ = setup_job(monkeypatch)
    first = MutationLease(store, [FILE_ID])
    first.claim()
    first.pin()
    first.refs[0].set({"leaseExpiresAt": datetime.now(UTC) - timedelta(days=1)}, merge=True)
    with pytest.raises(HTTPException):
        MutationLease(store, [FILE_ID]).claim()
    first.finish()
    assert MutationLease(store, [FILE_ID]).claim()


def test_ingest_and_other_index_jobs_cannot_change_inflight_document(monkeypatch):
    store, _, body = setup_job(monkeypatch)
    lease = MutationLease(store, [FILE_ID], versions=body.file_versions)
    lease.claim()
    lease.pin()
    with pytest.raises(HTTPException):
        with document_mutation(store, store.settings, [FILE_ID]):
            pytest.fail("must not mutate")
    with pytest.raises(HTTPException):
        sync_main._ingest_with(
            sync_main.IngestBody(fileId=FILE_ID, driveId="drive"),
            settings=store.settings,
            store=store,
            gcs=None,
            drive=None,
        )


def test_uncertain_rpc_retains_document_lock_and_fails_job(monkeypatch):
    store, job, body = setup_job(monkeypatch)

    def importing(*_a, **_kw):
        raise DeadlineExceeded("RPC may still be running")

    monkeypatch.setattr(sync_main, "_import_and_mark", importing)
    with pytest.raises(DeadlineExceeded):
        sync_main.index_gcs_task(body)
    assert job.get().to_dict()["status"] == "FAILED"
    assert store._tokens.document(f"__mutation__{FILE_ID}").get().to_dict()["phase"] == "UNCERTAIN"
    assert store.get(FILE_ID).status == DocStatus.PARSED


def test_all_parts_required_and_done_redelivery_recovers_finalizer(monkeypatch):
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda *_a, **_k: ImportOutcome(body.gcs_uris, 1, 0, 0)
    )
    assert sync_main.index_gcs_task(body)["status"] == "RUNNING"
    assert store.get(FILE_ID).status == DocStatus.PARSED
    job.collection("parts").document("student").set(
        {"status": "DONE", "result": {"ok": True}}, merge=True
    )
    assert sync_main.index_gcs_task(body)["status"] == "DONE"
    assert store.get(FILE_ID).status == DocStatus.INDEXED


def test_payload_and_routing_changes_are_rejected(monkeypatch):
    store, _, body = setup_job(monkeypatch)
    body.file_versions = {FILE_ID: "forged"}
    with pytest.raises(HTTPException):
        sync_main.index_gcs_task(body)
    store, job, body = setup_job(monkeypatch)
    store.settings.rag_corpus_name = "another-corpus"
    assert sync_main.index_gcs_task(body)["status"] == "FAILED"
    assert job.get().to_dict()["status"] == "FAILED"


def test_old_owner_does_not_fail_successors_job(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    original_pin = MutationLease.pin
    successors = []

    def take_over_before_pin(first):
        expired = datetime.now(UTC) - timedelta(seconds=1)
        for ref in [*first.refs, first.part_ref]:
            ref.set({"leaseExpiresAt": expired}, merge=True)
        second = MutationLease(
            store, [FILE_ID], versions=body.file_versions, job_ref=job, part_ref=first.part_ref
        )
        second.claim()
        successors.append(second)
        original_pin(first)

    monkeypatch.setattr(MutationLease, "pin", take_over_before_pin)
    with pytest.raises(HTTPException) as error:
        sync_main.index_gcs_task(body)
    assert error.value.status_code == 409
    assert job.get().to_dict()["status"] == "RUNNING"
    assert successors[0].part_ref.get().to_dict()["owner"] == successors[0].owner


def test_polling_recovers_completed_parts_and_does_not_resurrect_failed_job(monkeypatch):
    store, job, _ = setup_job(monkeypatch)
    job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    assert sync_main.index_job_status("job")["status"] == "DONE"
    assert store.get(FILE_ID).status == DocStatus.INDEXED
    store, job, _ = setup_job(monkeypatch)
    job.set({"status": "FAILED", "error": "deadline expired"}, merge=True)
    job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    assert sync_main.index_job_status("job")["status"] == "FAILED"
    assert store.get(FILE_ID).status == DocStatus.PARSED


def test_legacy_unversioned_task_fails_without_mutation(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    body.file_versions = {}
    job.set({"fileVersions": None}, merge=True)
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda *_a, **_kw: pytest.fail("unversioned import")
    )
    assert sync_main.index_gcs_task(body)["status"] == "FAILED"
    assert store.get(FILE_ID).status == DocStatus.PARSED
