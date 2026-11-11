import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from google.api_core.exceptions import DeadlineExceeded
from starlette.exceptions import HTTPException

import services.sync.main as sync_main
from shared.index_guard import MutationLease, StaleIndexTask, document_mutation, mutation_lock_names
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
    lock = store._tokens.document(f"__mutation__{FILE_ID}__faculty").get().to_dict()
    assert lock["phase"] == "UNCERTAIN"
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

def test_partial_batch_checkpoints_success_and_retries_only_failed_document(monkeypatch):
    from shared.models import DocState
    from shared.index_guard import document_version

    store, job, body = setup_job(monkeypatch)
    second = "another_document_id"
    store.upsert(DocState(file_id=second, drive_id="drive", status=DocStatus.PARSED,
                          content_hash="v1", modified_time="2026-09-01T00:00:00Z"))
    body.file_ids.append(second)
    body.gcs_uris.append(f"gs://source/{second}.md")
    body.file_versions[second] = document_version(store.get(second))
    job.set({"fileIds": body.file_ids, "gcsUris": body.gcs_uris,
             "fileVersions": body.file_versions}, merge=True)
    calls = []
    fail_second = True

    def importing(_store, uris, ids, **kwargs):
        calls.append(tuple(ids))
        bad = second in ids and fail_second
        return ImportOutcome(uris, 0 if bad else len(uris), 1 if bad else 0, 0)

    monkeypatch.setattr(sync_main, "_import_and_mark", importing)
    outcome = sync_main.index_gcs_task(body)
    # 묶음으로 한 번 반입하고, 부분 실패면 그 묶음만 파일별로 다시 돌린다.
    assert calls == [(FILE_ID, second), (FILE_ID,), (second,)]
    calls.clear()
    assert outcome["partial"] is True
    assert outcome["failedFileIds"] == [second]
    assert outcome["completedFileIds"] == [FILE_ID]
    assert store.get(FILE_ID).status == DocStatus.INDEXED
    assert store.get(second).status == DocStatus.PARSED
    results = job.collection("parts").document("faculty").get().to_dict()["fileResults"]
    assert results[FILE_ID]["status"] == "DONE"
    assert results[second]["status"] == "FAILED"
    fail_second = False
    assert sync_main.index_gcs_task(body)["status"] == "DONE"
    assert calls == []  # redelivery never replays the finished batch
    assert store.get(second).status == DocStatus.PARSED
    assert store._col.document(second).collection("index_failures").document("job").get().exists


def test_file_checkpoint_requires_both_corpora(monkeypatch):
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    monkeypatch.setattr(sync_main, "_import_and_mark",
                        lambda _store, uris, ids, **kw: ImportOutcome(uris, 1, 0, 0))
    assert sync_main.index_gcs_task(body)["status"] == "RUNNING"
    assert store.get(FILE_ID).status == DocStatus.PARSED
    body.audience, body.part_id = "STUDENT", "student"
    monkeypatch.setattr(sync_main, "_sync_student_corpus",
                        lambda *a: {"ok": False, "imported": 0})
    assert sync_main.index_gcs_task(body)["partial"] is True
    assert store.get(FILE_ID).status == DocStatus.PARSED
    monkeypatch.setattr(sync_main, "_sync_student_corpus",
                        lambda *a: {"ok": True, "imported": 1})
    assert sync_main.index_gcs_task(body)["status"] == "DONE"
    assert store.get(FILE_ID).status == DocStatus.PARSED

def test_checkpoint_stores_document_results_separately(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    monkeypatch.setattr(sync_main, "_import_and_mark",
                        lambda _store, uris, ids, **kw: ImportOutcome(uris, 0, 1, 0))
    assert sync_main.index_gcs_task(body)["partial"] is True
    ref=store._col.document(FILE_ID).collection("index_results").document("job")
    assert ref.get().to_dict()["status"] == "FAILED"
    monkeypatch.setattr(sync_main, "_import_and_mark",
                        lambda _store, uris, ids, **kw: ImportOutcome(uris, 1, 0, 0))
    sync_main.index_gcs_task(body)
    assert ref.get().to_dict()["status"] == "FAILED"
    assert ref.get().to_dict()["fileVersion"] == body.file_versions[FILE_ID]

def test_reconcile_accepts_only_server_persisted_deferred_files(monkeypatch):
    store, job, body = setup_job(monkeypatch)
    monkeypatch.setattr(sync_main, "_import_and_mark",
                        lambda _store, uris, ids, **kw: ImportOutcome(uris, 0, 1, 0))
    sync_main.index_gcs_task(body)
    payload = sync_main.ReconcileBody(driveId="drive", listed=1, gcsUploaded=1,
        uris=1, indexed=0, failed=0, skipped=0, deleted=0, indexJobId="job", deferredUris=1)
    assert sync_main.reconcile(payload)["ok"] is True
    payload.drive_id = "another-drive"
    assert sync_main.reconcile(payload)["ok"] is False
    payload.drive_id = "drive"
    payload.index_job_id = ""
    assert sync_main.reconcile(payload)["ok"] is False


def test_student_success_does_not_override_failed_faculty_checkpoint(monkeypatch):
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    monkeypatch.setattr(sync_main, "_import_and_mark",
                        lambda _store, uris, ids, **kw: ImportOutcome(uris, 0, 1, 0))
    assert sync_main.index_gcs_task(body)["status"] == "RUNNING"
    body.audience, body.part_id = "STUDENT", "student"
    monkeypatch.setattr(sync_main, "_sync_student_corpus", lambda *a: {"ok":True,"imported":1})
    result=sync_main.index_gcs_task(body)
    assert result["partial"] is True
    assert store.get(FILE_ID).status == DocStatus.PARSED
    assert store._col.document(FILE_ID).collection("index_results").document("job").get().to_dict()["status"]=="FAILED"


def _part_lease(store, job, body, part_id):
    return MutationLease(store, [FILE_ID], versions=body.file_versions, job_ref=job,
                         part_ref=job.collection("parts").document(part_id))


def test_faculty_and_student_parts_of_one_document_run_concurrently(monkeypatch):
    """두 코퍼스는 서로 다르므로 같은 문서라도 교직원·학생 part가 동시에 돈다."""
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    faculty = _part_lease(store, job, body, "faculty")
    assert faculty.claim()
    faculty.pin()
    student = _part_lease(store, job, body, "student")
    assert student.claim()
    student.pin()
    faculty.finish({"audience": "FACULTY"})
    student.finish({"audience": "STUDENT"})


@pytest.mark.parametrize("part_id", ["faculty", "student"])
def test_ingest_waits_for_any_running_index_part(monkeypatch, part_id):
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    lease = _part_lease(store, job, body, part_id)
    lease.claim()
    lease.pin()
    with pytest.raises(HTTPException) as error, document_mutation(store, store.settings, [FILE_ID]):
        pytest.fail("must not mutate while an index part runs")
    assert error.value.status_code == 409
    lease.finish({"audience": part_id.upper()})
    with document_mutation(store, store.settings, [FILE_ID]):
        pass


def test_index_part_waits_for_ingest_and_for_its_own_part(monkeypatch):
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    ingest = MutationLease(store, [FILE_ID])
    ingest.claim()
    ingest.pin()
    with pytest.raises(HTTPException):
        _part_lease(store, job, body, "faculty").claim()
    ingest.finish()
    first = _part_lease(store, job, body, "faculty")
    assert first.claim()
    first.pin()
    job.collection("parts").document("faculty").set({"phase": "IDLE", "status": "QUEUED"}, merge=True)
    with pytest.raises(HTTPException):
        _part_lease(store, job, body, "faculty").claim()


def test_finalizer_waits_while_a_part_lock_remains(monkeypatch):
    store, job, _ = setup_job(monkeypatch)
    job.collection("parts").document("faculty").set({"status": "DONE", "count": 1}, merge=True)
    store._tokens.document(f"__mutation__{FILE_ID}__faculty").set({"owner": "x", "phase": "UNCERTAIN"})
    assert sync_main._finalize_index_job(store.settings, "job")["status"] == "RUNNING"


def test_index_groups_respect_vertex_uri_limit():
    uris = {f"f{i}": ["a", "b"] for i in range(14)}
    uris["empty"] = []
    uris["huge"] = ["x"] * 30
    order = ["f0", "empty", *[f"f{i}" for i in range(1, 14)], "huge"]
    groups = sync_main._index_file_groups(order, uris)
    assert ["empty"] in groups and ["huge"] in groups
    batched = [g for g in groups if g not in (["empty"], ["huge"])]
    assert all(sum(len(uris[f]) for f in g) <= sync_main.MAX_IMPORT_URIS for g in batched)
    assert [f for g in batched for f in g] == ["f0", *[f"f{i}" for i in range(1, 14)]]
    assert len(batched) == 2  # 28 URIs -> 24 + 4

def test_faculty_and_student_tasks_overlap_end_to_end(monkeypatch):
    """두 task가 서로를 기다리지 않는다: 둘 다 반입 단계에 동시에 들어가야 끝난다."""
    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    both_inside = Barrier(2)

    def faculty_import(_store, uris, ids, **_kw):
        both_inside.wait(timeout=5)
        return ImportOutcome(uris, len(uris), 0, 0)

    def student_sync(uris, ids, _settings, _store):
        both_inside.wait(timeout=5)
        return {"enabled": True, "ok": True, "imported": 1, "removed": 0}

    monkeypatch.setattr(sync_main, "_import_and_mark", faculty_import)
    monkeypatch.setattr(sync_main, "_sync_student_corpus", student_sync)
    student_body = body.model_copy(update={"audience": "STUDENT", "part_id": "student"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(sync_main.index_gcs_task, b) for b in (body, student_body)]
        statuses = [r.result(timeout=10)["status"] for r in results]
    # 먼저 끝난 쪽은 RUNNING, 나중에 끝난 쪽이 작업을 DONE으로 확정한다.
    assert "DONE" in statuses
    assert job.get().to_dict()["status"] == "DONE"
    assert store.get(FILE_ID).status == DocStatus.INDEXED


def test_student_part_syncs_a_group_in_one_call(monkeypatch):
    from shared.index_guard import document_version
    from shared.models import DocState

    store, job, body = setup_job(monkeypatch, parts=("faculty", "student"))
    extra = [f"doc_{i:02d}_aaaaaaaaaaaaaaaaaaaa" for i in range(5)]
    for fid in extra:
        store.upsert(DocState(file_id=fid, drive_id="drive", status=DocStatus.PARSED,
                              content_hash="v1", modified_time="2026-09-01T00:00:00Z"))
        body.file_ids.append(fid)
        body.gcs_uris.append(f"gs://source/{fid}.md")
        body.file_versions[fid] = document_version(store.get(fid))
    job.set({"fileIds": body.file_ids, "gcsUris": body.gcs_uris,
             "fileVersions": body.file_versions}, merge=True)
    calls = []

    def student_sync(uris, ids, _settings, _store):
        calls.append(list(ids))
        return {"enabled": True, "ok": True, "imported": 3, "removed": 2}

    monkeypatch.setattr(sync_main, "_sync_student_corpus", student_sync)
    student_body = body.model_copy(update={"audience": "STUDENT", "part_id": "student"})
    sync_main.index_gcs_task(student_body)
    assert calls == [body.file_ids]
    part = job.collection("parts").document("student").get().to_dict()
    assert part["count"] == 3 and part["result"]["removed"] == 2
    assert all(r["status"] == "DONE" for r in part["fileResults"].values())

@pytest.mark.parametrize("fid", ["1fH3Bvp5qy4-vPa2vUATm71PfpsyMBg__", "plain_id", "__", "x_"])
def test_lock_names_avoid_firestore_reserved_ids(fid):
    names = mutation_lock_names(fid)
    assert len(set(names)) == len(names)
    assert not any(re.fullmatch(r"__.*__", name) for name in names)
    if not fid.endswith("_"):
        assert names[0] == f"__mutation__{fid}"
