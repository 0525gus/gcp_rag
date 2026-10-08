"""Rebuild historical indexing reviews from persisted jobs and import evidence.

Default is read-only. --apply writes only sync_run_reviews, never historical
Workflow states, document states, task checkpoints, or RAG corpus contents.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore, storage
from google.oauth2.credentials import Credentials

from shared.index_guard import document_version
from shared.index_results import classify_files, summary
from shared.models import DocState
from shared.rag_import_result import parse_import_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="asia-northeast3")
    parser.add_argument("--database", default="rag-sync-state")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    cli = shutil.which("gcloud.cmd") or shutil.which("gcloud")

    def gc(*arguments):
        return json.loads(
            subprocess.check_output(
                [cli, *arguments, "--project=" + args.project, "--format=json"],
                text=True,
                encoding="utf-8",
            )
        )

    token = subprocess.check_output([cli, "auth", "print-access-token"], text=True).strip()
    credentials = Credentials(token)
    db = firestore.Client(project=args.project, database=args.database, credentials=credentials)
    gcs = storage.Client(project=args.project, credentials=credentials)
    service = gc("run", "services", "describe", "rag-sync", "--region=" + args.region)
    env = {
        x["name"]: x.get("value", "")
        for x in service["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    departments = json.loads(env.get("DEPARTMENTS_JSON") or "{}")
    targets = {
        drive: (code, config)
        for code, config in departments.items()
        for drive in config.get("driveIds", [])
    }
    runs = gc(
        "workflows",
        "executions",
        "list",
        "rag-daily-sync",
        "--location=" + args.region,
        "--limit=" + str(args.limit),
        "--sort-by=~startTime",
    )

    def describe(row):
        return gc(
            "workflows",
            "executions",
            "describe",
            row["name"].rsplit("/", 1)[-1],
            "--workflow=rag-daily-sync",
            "--location=" + args.region,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(describe, [r for r in runs if r.get("state") == "FAILED"]))
    reviews = []
    for run in runs:
        execution = run["name"].rsplit("/", 1)[-1]
        error = str((run.get("error") or {}).get("context") or "")
        match = re.search(r"jobId=([a-f0-9]{32})", error)
        if not match:
            reviews.append(
                {
                    "executionId": execution,
                    "reviewStatus": "UNAVAILABLE",
                    "reason": "색인 작업 ID를 확인할 수 없습니다.",
                }
            )
            continue
        job_id = match.group(1)
        ref = db.collection("sync_jobs").document(job_id)
        job = ref.get(timeout=20).to_dict() or {}
        code, config = targets.get(job.get("driveId"), ("", {}))
        if not job or not code:
            reviews.append(
                {
                    "executionId": execution,
                    "reviewStatus": "UNAVAILABLE",
                    "reason": "작업 또는 학과 범위를 확인할 수 없습니다.",
                }
            )
            continue
        parts = {
            pid: ref.collection("parts").document(pid).get(timeout=20).to_dict() or {}
            for pid in job.get("expectedParts", [])
        }
        refs = [db.collection("doc_state").document(fid) for fid in job.get("fileIds", [])]
        docs = {snap.id: snap.to_dict() for snap in db.get_all(refs, timeout=30) if snap.exists}
        versions = {
            fid: doc.get("driveId") == job.get("driveId")
            and document_version(DocState.from_firestore(doc))
            == (job.get("fileVersions") or {}).get(fid)
            for fid, doc in docs.items()
        }
        # Document names from another department must never leak into this view.
        docs = {fid: doc for fid, doc in docs.items() if doc.get("driveId") == job.get("driveId")}
        start = job.get("createdAt")
        end = run.get("endTime")
        sinks = {}
        notices = []
        corpus_to_part = {
            str(config.get(key) or "").rsplit("/", 1)[-1]: pid
            for key, pid in [("staffCorpus", "faculty"), ("studentCorpus", "student")]
            if config.get(key)
        }
        if start and end:
            page = ""
            for _ in range(20):
                body = {
                    "resourceNames": ["projects/" + args.project],
                    "filter": 'resource.type="cloud_run_revision" AND resource.labels.service_name="rag-sync" AND textPayload:"RAG import result sink read" AND timestamp>="'
                    + start.isoformat()
                    + '" AND timestamp<="'
                    + end
                    + '"',
                    "orderBy": "timestamp asc",
                    "pageSize": 1000,
                }
                if page:
                    body["pageToken"] = page
                req = urllib.request.Request(
                    "https://logging.googleapis.com/v2/entries:list",
                    data=json.dumps(body).encode(),
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=40) as response:
                    data = json.load(response)
                for entry in data.get("entries", []):
                    found = re.search(r"sink=(gs://[^\s]+)", entry.get("textPayload", ""))
                    if not found:
                        continue
                    uri = found.group(1)
                    path = urlparse(uri).path.strip("/").split("/")
                    if (
                        len(path) == 3
                        and path[0] == "import-results"
                        and path[1] in corpus_to_part
                        and urlparse(uri).netloc == env.get("RAG_METADATA_BUCKET")
                    ):
                        sinks[uri] = (entry["timestamp"], corpus_to_part[path[1]])
                page = data.get("nextPageToken", "")
                if not page:
                    break
            if page:
                notices.append("결과 로그 조회 한도를 초과했습니다.")
        evidence = {}
        for uri, (timestamp, pid) in sorted(sinks.items(), key=lambda pair: pair[1][0]):
            parsed = urlparse(uri)
            try:
                payload = (
                    gcs.bucket(parsed.netloc)
                    .blob(parsed.path.lstrip("/"))
                    .download_as_bytes(timeout=30)
                )
                for item in parse_import_results(payload, sink_uri=uri):
                    if item.gcs_uri not in job.get("gcsUris", []):
                        continue
                    status = (
                        "DONE"
                        if item.succeeded
                        else "FAILED"
                        if item.error
                        or item.status.upper() in {"FAILED", "ERROR", "INVALID_ARGUMENT"}
                        else "UNKNOWN"
                    )
                    evidence[(pid, item.gcs_uri)] = {
                        "status": status,
                        "error": item.error or "",
                        "sink": uri,
                        "timestamp": timestamp,
                    }
            except (GoogleAPICallError, OSError, ValueError, TypeError) as exc:
                notices.append("반입 결과 파일 조회 실패: " + type(exc).__name__)
        files = classify_files(job, parts, docs, evidence, version_matches=versions)
        review = {
            "executionId": execution,
            "jobId": job_id,
            "departmentCode": code,
            "driveId": job.get("driveId"),
            "workflowState": "FAILED",
            "reviewStatus": "REVIEWED",
            "scope": "FAILED_INDEX_JOB",
            "checkedAt": datetime.now(UTC).isoformat(),
            "files": files,
            "counts": summary(files),
            "sourceError": error,
            "notices": notices,
            "evidenceCount": len(evidence),
            "evidence": [{"part": pid, "uri": uri, **row} for (pid, uri), row in evidence.items()],
        }
        reviews.append(review)
        print(
            json.dumps(
                {
                    "executionId": execution,
                    "jobId": job_id,
                    "counts": review["counts"],
                    "evidenceCount": len(evidence),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    Path(args.output).write_text(
        json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.apply:
        for review in reviews:
            if review.get("reviewStatus") == "REVIEWED":
                ref = db.collection("sync_run_reviews").document(review["executionId"])

                @firestore.transactional
                def save_review(transaction, ref=ref, review=review):
                    previous = ref.get(transaction=transaction).to_dict() or {}
                    dismissed = previous.get("dismissedFileIds") or []
                    files = [row for row in review["files"] if row["fileId"] not in dismissed]
                    transaction.set(
                        ref,
                        {
                            **review,
                            "files": files,
                            "counts": summary(files),
                            "dismissedFileIds": dismissed,
                            "dismissal": previous.get("dismissal"),
                        },
                    )

                save_review(db.transaction())
        print("Saved historical reviews; original states unchanged.", flush=True)


if __name__ == "__main__":
    main()
