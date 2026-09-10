"""Firestore ownership and revision checks for document mutations.

An expired CLAIMED lease can be taken over. MUTATING leases cannot: Vertex RAG
does not accept fencing tokens, so an RPC may still be running after its caller
dies. Those leases require reconciliation before an operator releases them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.exceptions import HTTPException
from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from shared.models import DocState


class StaleIndexTask(RuntimeError):
    pass


class MutationOwnershipLost(StaleIndexTask):
    """A superseded worker must not change its successor's task/job state."""


def document_version(state: DocState) -> str:
    data = state.to_firestore()
    # INDEXED/PARSED transitions and timestamps do not create a new revision.
    for field in ("status", "lastSyncedAt", "error", "ragFileId"):
        data.pop(field, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def routing_version(settings: Any) -> str:
    fields = (
        "rag_corpus_name",
        "rag_corpus_name_student",
        "gcs_source_bucket",
        "gcs_hwp_original_bucket",
        "sync_folder_ids",
        "student_folder_ids",
    )
    return hashlib.sha256(
        json.dumps({name: getattr(settings, name, "") for name in fields}, sort_keys=True).encode()
    ).hexdigest()


def check_versions(store: Any, versions: dict[str, str], txn: Any) -> None:
    if not versions:
        raise StaleIndexTask("index job has no document versions; enqueue it again")
    for fid, expected in versions.items():
        snap = store._col.document(fid).get(transaction=txn)
        data = snap.to_dict() or {}
        data.setdefault("fileId", fid)
        if (
            not snap.exists
            or data.get("status") not in {"PARSED", "INDEXED"}
            or document_version(DocState.from_firestore(data)) != expected
        ):
            raise StaleIndexTask(f"document revision changed: {fid}")


def _uncertain(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(
            exc,
            (
                TimeoutError,
                ConnectionError,
                gcp_exceptions.DeadlineExceeded,
                gcp_exceptions.ServiceUnavailable,
                gcp_exceptions.InternalServerError,
            ),
        ):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class MutationLease:
    def __init__(
        self,
        store: Any,
        file_ids: list[str],
        *,
        versions: dict[str, str] | None = None,
        job_ref: Any = None,
        part_ref: Any = None,
    ) -> None:
        self.store = store
        self.file_ids = sorted(set(file_ids))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", fid) for fid in self.file_ids):
            raise HTTPException(422, "invalid document ID for mutation")
        self.versions = versions
        self.job_ref = job_ref
        self.part_ref = part_ref
        self.owner = uuid.uuid4().hex
        self.refs = [store._tokens.document(f"__mutation__{fid}") for fid in self.file_ids]
        self.pinned = False

    def claim(self) -> bool:
        @firestore.transactional
        def run(txn: Any) -> bool:
            now = datetime.now(UTC)
            if self.part_ref is not None:
                job = self.job_ref.get(transaction=txn).to_dict() or {}
                part = self.part_ref.get(transaction=txn).to_dict() or {}
                if not job or not part:
                    raise HTTPException(404, "index job/part not found")
                if part.get("status") == "DONE":
                    return False
                if job.get("status") in {"FAILED", "DONE"}:
                    raise StaleIndexTask("index job is already terminal")
                if job.get("deadlineAt") and job["deadlineAt"] <= now:
                    raise StaleIndexTask("index job deadline expired")
                if part.get("status") == "RUNNING":
                    if part.get("phase") == "MUTATING" or part.get("leaseExpiresAt", now) > now:
                        raise HTTPException(409, "index part is already running")
            if self.versions is not None:
                check_versions(self.store, self.versions, txn)
            for ref in self.refs:
                data = ref.get(transaction=txn).to_dict() or {}
                if data.get("owner") and (
                    data.get("phase") in {"MUTATING", "UNCERTAIN"}
                    or data.get("leaseExpiresAt", now) > now
                ):
                    raise HTTPException(409, "document mutation is busy; retry later")
            payload = {
                "owner": self.owner,
                "phase": "CLAIMED",
                "leaseExpiresAt": now + timedelta(seconds=120),
                "updatedAt": firestore.SERVER_TIMESTAMP,
                "jobPath": self.job_ref.path if self.job_ref is not None else None,
                "partPath": self.part_ref.path if self.part_ref is not None else None,
            }
            for ref in self.refs:
                txn.set(ref, payload)
            if self.part_ref is not None:
                txn.set(self.part_ref, {**payload, "status": "RUNNING"}, merge=True)
                for fid in self.file_ids:
                    # Reindexing an INDEXED document must not leave a success
                    # status behind if deletion succeeds but import fails.
                    txn.update(self.store._col.document(fid), {"status": "PARSED"})
            return True

        return run(self.store._db.transaction())

    def _owned(self, txn: Any) -> None:
        now = datetime.now(UTC)
        for ref in [*self.refs, *([self.part_ref] if self.part_ref is not None else [])]:
            data = ref.get(transaction=txn).to_dict() or {}
            if data.get("owner") != self.owner:
                raise MutationOwnershipLost("mutation ownership lost")
            if data.get("phase") == "CLAIMED" and data.get("leaseExpiresAt", now) <= now:
                raise MutationOwnershipLost("mutation lease expired before starting")
        if self.job_ref is not None:
            job = self.job_ref.get(transaction=txn).to_dict() or {}
            if job.get("status") != "RUNNING":
                raise StaleIndexTask("index job is no longer running")
            if job.get("deadlineAt") and job["deadlineAt"] <= now:
                raise StaleIndexTask("index job deadline expired")
        if self.versions is not None:
            check_versions(self.store, self.versions, txn)

    def check(self) -> None:
        @firestore.transactional
        def run(txn: Any) -> None:
            self._owned(txn)

        run(self.store._db.transaction())

    def pin(self) -> None:
        @firestore.transactional
        def run(txn: Any) -> None:
            self._owned(txn)
            for ref in [*self.refs, *([self.part_ref] if self.part_ref is not None else [])]:
                txn.set(ref, {"phase": "MUTATING"}, merge=True)

        run(self.store._db.transaction())
        self.pinned = True

    def finish(
        self, result: dict[str, Any] | None = None, error: BaseException | None = None
    ) -> None:
        @firestore.transactional
        def run(txn: Any) -> None:
            owned = []
            for ref in self.refs:
                if (ref.get(transaction=txn).to_dict() or {}).get("owner") == self.owner:
                    owned.append(ref)
            part = (
                self.part_ref.get(transaction=txn).to_dict() if self.part_ref is not None else None
            )
            if result is not None:
                if len(owned) != len(self.refs) or not part or part.get("owner") != self.owner:
                    raise MutationOwnershipLost("mutation ownership lost before completion")
                check_versions(self.store, self.versions or {}, txn)
            uncertain = self.pinned and _uncertain(error)
            for ref in owned:
                if uncertain:
                    txn.set(
                        ref,
                        {"phase": "UNCERTAIN", "error": "RPC outcome requires reconciliation"},
                        merge=True,
                    )
                else:
                    txn.delete(ref)
            if part and part.get("owner") == self.owner:
                txn.set(
                    self.part_ref,
                    {
                        **(result or {}),
                        "status": "DONE"
                        if result is not None
                        else ("FAILED" if uncertain else "RETRYING"),
                        "owner": None,
                        "phase": "UNCERTAIN" if uncertain else "IDLE",
                        "error": str(error)[:2000] if error else None,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                        "completedAt": firestore.SERVER_TIMESTAMP if result is not None else None,
                    },
                    merge=True,
                )
                if uncertain:
                    txn.set(
                        self.job_ref,
                        {
                            "status": "FAILED",
                            "error": "RPC outcome uncertain; reconcile document locks",
                        },
                        merge=True,
                    )

        run(self.store._db.transaction())


_ACTIVE: ContextVar[MutationLease | None] = ContextVar("document_mutation", default=None)


@contextmanager
def activate(lease: MutationLease):
    token = _ACTIVE.set(lease)
    try:
        yield lease
    finally:
        _ACTIVE.reset(token)


@contextmanager
def document_mutation(store: Any, settings: Any, file_ids: list[str]):
    if not getattr(settings, "cloud_tasks_enabled", False):
        yield None
        return
    active = _ACTIVE.get()
    if active is not None:
        if not set(file_ids).issubset(active.file_ids):
            raise RuntimeError("nested mutation must use the acquired document set")
        active.check()
        yield active
        return
    lease = MutationLease(store, file_ids)
    lease.claim()
    try:
        lease.pin()
        with activate(lease):
            yield lease
    except BaseException as exc:
        lease.finish(error=exc)
        raise
    else:
        lease.finish()


def check_active_mutation() -> None:
    lease = _ACTIVE.get()
    if lease is not None:
        lease.check()
