"""Durable, bounded propagation of collector metadata without repeating OCR."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import HTTPException

from shared.drive import DriveClient
from shared.gcs import GcsClient
from shared.hashing import sha256_text
from shared.index_guard import document_mutation
from shared.models import DocStatus
from shared.path_context import build_breadcrumb_markdown
from shared.task_queue import IndexTaskQueue


def _source_ref(store, file_id):
    return store._db.collection(store.settings.sync_job_collection).document(
        "metadata-source-" + hashlib.sha256(file_id.encode()).hexdigest()[:32]
    )


def enqueue_page(settings, job_id, page_token=""):
    suffix = hashlib.sha256(page_token.encode()).hexdigest()[:16]
    return IndexTaskQueue(settings).enqueue(
        queue=settings.task_queue_faculty,
        task_id=f"{job_id}-{suffix}",
        payload={"jobId": job_id},
        endpoint="/sync/metadata-refresh-task",
    )


def schedule_change(change, token, store, settings, drive):
    """Persist jobs before the caller is allowed to commit the Drive change token."""
    source = _source_ref(store, change.file_id)
    previous = source.get().to_dict() or {}
    if change.name != "metadata.json" and not previous:
        return []
    if not settings.cloud_tasks_enabled:
        raise HTTPException(503, "metadata refresh requires Cloud Tasks")
    parents = set(previous.get("folderIds") or [])
    current_parents = set(change.parents or [])
    if not change.removed and change.name == "metadata.json" and not current_parents:
        current_parents.update(drive.get_parents(change.file_id))
    parents.update(current_parents)
    job_ids = []
    for parent in sorted(parents):
        if settings.sync_folder_id_list and not drive.is_in_sync_scope(
            parent, settings.sync_folder_id_list
        ):
            continue
        identity = json.dumps(
            [change.drive_id, change.file_id, parent, change.modified_time, change.removed, token],
            separators=(",", ":"),
        )
        job_id = "metadata-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        ref = store._db.collection(settings.sync_job_collection).document(job_id)
        if not ref.get().exists:
            ref.set(
                {
                    "kind": "METADATA_REFRESH",
                    "jobId": job_id,
                    "driveId": change.drive_id,
                    "folderId": parent,
                    "metadataFileId": change.file_id,
                    "status": "RUNNING",
                    "pageToken": "",
                    "createdAt": datetime.now(UTC),
                    "updatedAt": datetime.now(UTC),
                }
            )
        enqueue_page(settings, job_id)
        job_ids.append(job_id)
    source.set(
        {
            "kind": "METADATA_SOURCE",
            "driveId": change.drive_id,
            "fileId": change.file_id,
            "folderIds": sorted(current_parents or parents),
            "updatedAt": datetime.now(UTC),
        }
    )
    return job_ids


def refresh_file(meta, *, store, settings, drive, gcs, source_uris, index):
    """Update existing artifacts and their shared document metadata under a lease."""
    fid = meta["id"]
    with document_mutation(store, settings, [fid]):
        state = store.get(fid)
        if (
            not state
            or state.drive_id != meta.get("driveId")
            or state.status in {DocStatus.DELETED, DocStatus.EXCLUDED}
        ):
            return {"status": "IGNORED"}
        if settings.sync_folder_id_list and not drive.is_in_sync_scope(
            fid, settings.sync_folder_id_list
        ):
            return {"status": "IGNORED"}
        # Avoid writing old content over a newer Drive revision. Normal sync handles it.
        if meta.get("modifiedTime") and state.modified_time != meta["modifiedTime"]:
            return {"status": "CONTENT_CHANGED"}
        try:
            context = drive.resolve_path_context(
                fid, state.name or meta.get("name", fid), strict_metadata=True
            )
        except ValueError as exc:
            # No writes occurred: release the lease normally, retain valid old metadata.
            return {"status": "INVALID_METADATA", "error": str(exc)[:1000]}
        except Exception as exc:  # noqa: BLE001 - retry read failures outside the document lease
            return {"status": "RETRY", "error": str(exc)[:1000]}
        changed = (
            context.source_title != (state.source_title or "")
            or context.website_url != (state.website_url or "")
            or context.metadata_file_id != (state.metadata_file_id or "")
        )
        if not changed and state.status != DocStatus.PARSED:
            return {"status": "UNCHANGED"}
        refreshed = replace(
            state,
            source_title=context.source_title or None,
            website_url=context.website_url or None,
            metadata_file_id=context.metadata_file_id or None,
        )
        if state.status not in {DocStatus.INDEXED, DocStatus.PARSED}:
            store.upsert(refreshed)
            return {"status": state.status.value}
        uris = source_uris(settings, fid, gcs)
        if not uris:
            raise RuntimeError("indexed document has no cached source artifacts")
        for uri in uris:
            if not uri.endswith(".md"):
                continue
            cached = gcs.download_bytes(uri).decode("utf-8")
            rewritten = build_breadcrumb_markdown(
                path=context.path,
                bundle=context.bundle,
                title=state.name or fid,
                source_title=context.source_title,
                website_url=context.website_url,
                body=cached,
            )
            bucket, blob = uri[5:].split("/", 1)
            gcs.upload_bytes(rewritten.encode(), bucket, blob, "text/markdown")
        refreshed.status = DocStatus.PARSED
        if changed:
            refreshed.content_hash = sha256_text(
                json.dumps(
                    [
                        state.content_hash,
                        context.source_title,
                        context.website_url,
                        context.metadata_file_id,
                    ],
                    ensure_ascii=False,
                )
            )
        store.upsert(refreshed)
        result = index(uris, [fid], state.drive_id)
        if result.get("failed") or result.get("ok") is False:
            raise RuntimeError("metadata reindex incomplete")
        return {"status": "INDEXED", "result": result}


def run_page(job_id, *, base, settings_for_drive, source_uris, index):
    from shared.firestore_state import DocStateStore

    store = DocStateStore(base)
    ref = store._db.collection(base.sync_job_collection).document(job_id)
    job = ref.get().to_dict() or {}
    if job.get("kind") != "METADATA_REFRESH":
        raise HTTPException(404, "Metadata job not found")
    if job.get("status") == "DONE":
        return {"jobId": job_id, "status": "DONE"}
    lock = "metadata-refresh:" + job_id
    if not store.try_acquire_lock(lock, ttl_seconds=1800):
        raise HTTPException(409, "Metadata job already running")
    try:
        job = ref.get().to_dict() or {}
        if job.get("status") == "DONE":
            return {"jobId": job_id, "status": "DONE"}
        settings = settings_for_drive(base, job["driveId"])
        drive = DriveClient()
        gcs = GcsClient(settings)
        if settings.sync_folder_id_list and not drive.is_in_sync_scope(
            job["folderId"], settings.sync_folder_id_list
        ):
            ref.set({"status": "DONE", "reason": "out_of_folder_scope"}, merge=True)
            return {"jobId": job_id, "status": "DONE"}
        page = drive.list_folder_page(job["folderId"], job.get("pageToken") or "")
        failures = []
        for meta in page.get("files", []):
            if (
                meta.get("name") == "metadata.json"
                or meta.get("mimeType") == "application/vnd.google-apps.folder"
            ):
                continue
            if meta.get("driveId") != job["driveId"]:
                continue
            item = ref.collection("files").document(meta["id"])
            try:
                result = refresh_file(
                    meta,
                    store=store,
                    settings=settings,
                    drive=drive,
                    gcs=gcs,
                    source_uris=source_uris,
                    index=index,
                )
                item.set({**result, "updatedAt": datetime.now(UTC)})
                if result.get("status") == "RETRY":
                    failures.append(meta["id"])
            except Exception as exc:  # noqa: BLE001 - preserve other files and retry this page
                item.set(
                    {"status": "FAILED", "error": str(exc)[:1000], "updatedAt": datetime.now(UTC)}
                )
                failures.append(meta["id"])
        if failures:
            raise RuntimeError(f"metadata refresh failed for {len(failures)} file(s)")
        next_token = page.get("nextPageToken") or ""
        if next_token:
            enqueue_page(base, job_id, next_token)
        ref.set(
            {
                "status": "RUNNING" if next_token else "DONE",
                "pageToken": next_token,
                "error": None,
                "updatedAt": datetime.now(UTC),
            },
            merge=True,
        )
        return {"jobId": job_id, "status": "RUNNING" if next_token else "DONE"}
    except Exception as exc:
        ref.set(
            {"status": "FAILED", "error": str(exc)[:1000], "updatedAt": datetime.now(UTC)},
            merge=True,
        )
        raise HTTPException(503, "metadata refresh failed; task will retry") from exc
    finally:
        store.release_lock(lock)
