"""Read-only, department-scoped views of unresolved document indexing state."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from urllib.parse import quote

from google.cloud.firestore_v1.base_query import FieldFilter

LABELS = {
    "PENDING": "처리 대기", "PARSED": "색인 완료 미확인", "FAILED": "처리 오류",
    "SKIPPED": "처리 보류", "SPLIT_QUEUED": "분할 대기", "BODY_MISSING": "본문 없이 색인",
}
DEFAULT_REASONS = {
    "PENDING": "아직 처리 완료 상태가 기록되지 않았습니다.",
    "PARSED": "파싱은 완료됐지만 색인 완료가 기록되지 않았습니다. 실행 중이거나 배치 일부 실패의 영향일 수 있습니다.",
    "FAILED": "개별 오류 사유가 기록되지 않았습니다. 상세에서 관련 작업을 확인하세요.",
    "SKIPPED": "처리가 보류됐습니다. 개별 사유가 기록되지 않았습니다.",
}


def timestamp(value):
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def issue_item(file_id, data):
    status = data.get("status", "PENDING")
    reason = str(data.get("error") or "")
    if status in {"EXCLUDED", "DELETED"} or reason == "out_of_folder_scope":
        return None
    if status == "INDEXED" and reason.startswith("NO_BODY_EXTRACTOR:"):
        status = "BODY_MISSING"
    if status not in LABELS:
        return None
    return {
        "fileId": file_id, "name": str(data.get("name") or file_id),
        "status": status, "statusLabel": LABELS[status],
        "reason": reason[:2000] or DEFAULT_REASONS.get(status, "본문 추출 없이 파일 정보만 색인했습니다."),
        "path": str(data.get("path") or ""), "mimeType": str(data.get("mimeType") or ""),
        "route": str(data.get("parseRoute") or ""), "audience": str(data.get("audience") or "STAFF"),
        "updatedAt": timestamp(data.get("lastSyncedAt") or data.get("updatedAt")),
        "sourceUrl": f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view",
    }


def list_issues(db, collection, drive_ids, *, cursor="", scan_limit=500, split_collection="doc_split_queue"):
    """Page through drive-scoped state without requiring a composite index.

    Normal documents are scanned but never returned. The cursor is scoped to the
    server-resolved drive list, and the UI reports counts for loaded pages only.
    """
    drives = sorted(set(drive_ids))
    position, after = 0, ""
    if cursor:
        try:
            saved = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            position, after = saved["position"], saved["after"]
            if (saved["drives"] != drives or type(position) is not int or not 0 <= position < len(drives)
                    or not isinstance(after, str) or len(after) > 1500 or "/" in after):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("조회 위치가 변경됐습니다. 새로고침해 주세요.") from exc
    rows, scanned = [], 0
    docs = db.collection(collection)
    while position < len(drives) and scanned < scan_limit:
        remaining = scan_limit - scanned
        query = docs.where(filter=FieldFilter("driveId", "==", drives[position])).order_by("__name__")
        if after:
            query = query.start_after({"__name__": docs.document(after)})
        page = list(query.limit(remaining + 1).stream(timeout=20))
        for snap in page[:remaining]:
            data = snap.to_dict() or {}
            # Keep the scope check even if a query adapter is changed later.
            if data.get("driveId") == drives[position]:
                item = issue_item(snap.id, data)
                if item:
                    rows.append(item)
            scanned += 1
            after = snap.id
        if len(page) > remaining:
            break
        position, after = position + 1, ""
    next_cursor = ""
    if position < len(drives):
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "position": position, "after": after, "drives": drives,
        }).encode()).decode()
    failed = {row["fileId"]: row for row in rows if row["status"] == "FAILED"}
    if failed:
        references = [db.collection(split_collection).document(fid) for fid in failed]
        for snap in db.get_all(references, timeout=20):
            data = snap.to_dict() or {}
            if snap.exists and data.get("driveId") in drives and snap.id in failed:
                failed[snap.id].update(status="SPLIT_QUEUED", statusLabel=LABELS["SPLIT_QUEUED"])
    return {"items": rows, "scanned": scanned, "nextCursor": next_cursor}


def issue_detail(db, names, drive_ids, file_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", file_id):
        raise FileNotFoundError(file_id)
    snap = db.collection(names["docState"]).document(file_id).get(timeout=20)
    data = snap.to_dict() or {}
    if not snap.exists or data.get("driveId") not in drive_ids:
        raise FileNotFoundError(file_id)
    item = issue_item(file_id, data)
    if not item:
        return {"resolved": True, "fileId": file_id, "message": "현재 보류·오류 대상이 아닙니다. 목록을 새로고침해 주세요."}
    notices, queues = [], []
    for key, label in (("dlq", "오류 재처리 대기"), ("splitQueue", "분할 대기")):
        queued = db.collection(names[key]).document(file_id).get(timeout=20)
        values = queued.to_dict() or {}
        # Recovered documents can retain old queue entries. Do not resurrect them.
        if queued.exists and data.get("status") == "FAILED" and values.get("driveId") == data.get("driveId"):
            queues.append({"label": label, "reason": str(values.get("reason") or "")[:2000],
                           "retryCount": values.get("retryCount", 0),
                           "updatedAt": timestamp(values.get("lastRetryAt") or values.get("enqueuedAt"))})
            if key == "splitQueue":
                item.update(status="SPLIT_QUEUED", statusLabel=LABELS["SPLIT_QUEUED"])
    # This is batch context, never evidence that every member failed individually.
    jobs = list(db.collection(names["jobs"]).where(
        filter=FieldFilter("fileIds", "array_contains", file_id),
    ).limit(101).stream(timeout=20))
    if len(jobs) > 100:
        notices.append("관련 작업이 많아 조회된 100건만 표시합니다. 전체 최신 이력은 실행 로그를 확인하세요.")
    matching = []
    for job in jobs[:100]:
        value = job.to_dict() or {}
        if value.get("driveId") == data.get("driveId") and value.get("kind") == "INDEX_GCS":
            matching.append((job, value))
    matching.sort(key=lambda pair: timestamp(pair[1].get("createdAt")), reverse=True)
    related = []
    for job, value in matching[:3]:
        parts = []
        for part_id in ("faculty", "student"):
            part = job.reference.collection("parts").document(part_id).get(timeout=20)
            info = part.to_dict() or {}
            if part.exists:
                parts.append({"audience": part_id, "status": str(info.get("status") or ""),
                              "error": str(info.get("error") or "")[:2000],
                              "fileResult": (info.get("fileResults") or {}).get(file_id)})
        related.append({"jobId": job.id, "status": str(value.get("status") or ""),
                        "error": str(value.get("error") or "")[:2000], "parts": parts,
                        "createdAt": timestamp(value.get("createdAt")),
                        "deadlineAt": timestamp(value.get("deadlineAt"))})
    return {"item": item, "queues": queues, "jobs": related, "notices": notices,
            "batchNotice": "관련 작업은 여러 파일을 묶은 배치입니다. 배치 실패가 이 파일 자체의 실패를 뜻하지는 않습니다."}
