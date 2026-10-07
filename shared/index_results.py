"""Conservative historical file-result classification; never rewrites Workflow state."""

from __future__ import annotations


def classify_files(job, parts, documents, evidence, *, version_matches):
    """Evidence is scoped to this job's time, corpus and exact requested URI.

    Metadata success alone never proves that the body succeeded. Historical
    evidence is not permission to change today's document or retry checkpoints.
    """
    rows = []
    for fid in dict.fromkeys(job.get("fileIds") or []):
        doc = documents.get(fid) or {}
        uris = [
            uri for uri in job.get("gcsUris", []) if uri.rsplit("/", 1)[-1].startswith(fid + ".")
        ]
        results = {}
        for pid in job.get("expectedParts") or []:
            part = parts.get(pid) or {}
            checkpoint = (part.get("fileResults") or {}).get(fid) or {}
            if checkpoint.get("status") in {"DONE", "FAILED"}:
                results[pid] = {
                    "status": checkpoint["status"],
                    "reason": checkpoint.get("error") or "",
                    "source": "checkpoint",
                }
                continue
            if part.get("status") == "DONE":
                results[pid] = {"status": "DONE", "reason": "", "source": "completed-part"}
                continue
            if not version_matches.get(fid):
                results[pid] = {
                    "status": "UNKNOWN",
                    "reason": "문서 버전이 달라 과거 대상을 확정할 수 없습니다.",
                }
                continue
            if pid == "student" and doc.get("audience", "STAFF") != "STUDENT":
                results[pid] = {
                    "status": "UNKNOWN",
                    "reason": "학생 코퍼스의 이전 자료 삭제 완료 여부를 확인할 수 없습니다.",
                }
                continue
            observed = [evidence.get((pid, uri)) for uri in uris]
            failures = [row for row in observed if row and row.get("status") == "FAILED"]
            if failures:
                results[pid] = {
                    "status": "FAILED",
                    "reason": "; ".join(
                        dict.fromkeys(row.get("error") or "반입 실패" for row in failures)
                    ),
                    "source": "import-result",
                }
            elif observed and all(row and row.get("status") == "DONE" for row in observed):
                results[pid] = {"status": "DONE", "reason": "", "source": "import-result"}
            else:
                results[pid] = {
                    "status": "UNKNOWN",
                    "reason": "본문을 포함한 모든 반입 결과가 확인되지 않았습니다.",
                }
        states = [row["status"] for row in results.values()]
        status = (
            "FAILED"
            if "FAILED" in states
            else "DONE"
            if states and all(s == "DONE" for s in states)
            else "PARTIAL"
            if "DONE" in states
            else "UNKNOWN"
        )
        rows.append(
            {
                "fileId": fid,
                "name": doc.get("name") or fid,
                "status": status,
                "corpora": results,
                "currentDocumentStatus": doc.get("status") or "UNKNOWN",
            }
        )
    return rows


def summary(rows):
    return {
        status: sum(row["status"] == status for row in rows)
        for status in ("DONE", "FAILED", "PARTIAL", "UNKNOWN")
    }
