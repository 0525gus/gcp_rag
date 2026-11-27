from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from scripts import gcp_cli, index_issues
from scripts.console import cloud_registry, core, sync_history

router = APIRouter()


def _index_issue_context(code: str):
    if not core.DEPT_CODE_RE.fullmatch(code):
        raise FileNotFoundError(code)
    config = cloud_registry.department_public_config_any(code)
    drives = core._normalise_ids((config.get("drive") or {}).get("driveIds"))
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    if not project:
        raise RuntimeError("공통 프로젝트 설정이 없습니다.")
    names = {**core._firestore_collection_names(), "jobs": str(common["SYNC_JOB_COLLECTION"])}
    return config, drives, common, names


def _index_issue_evidence(file_id: str, config: dict, common: dict, detail: dict) -> list[dict]:
    from datetime import timedelta

    from shared.rag_import_result import parse_import_results

    jobs = detail.get("jobs") or []
    metadata_bucket = str(common.get("RAG_METADATA_BUCKET") or "")
    source_bucket = str((config.get("buckets") or {}).get("source") or "")
    if not jobs or not core.BUCKET_RE.fullmatch(metadata_bucket) or not core.BUCKET_RE.fullmatch(source_bucket):
        return []
    job = jobs[0]
    start = datetime.fromisoformat(job["createdAt"])
    end = datetime.fromisoformat(job["deadlineAt"]) + timedelta(minutes=10)
    token = gcp_cli.access_token()
    status, body, _ = gcp_cli.http_post_json("https://logging.googleapis.com/v2/entries:list", {
        "resourceNames": [f"projects/{common['GCP_PROJECT_ID']}"],
        "filter": 'resource.type="cloud_run_revision" AND resource.labels.service_name="rag-sync" '
                  'AND textPayload:"RAG import result sink read" '
                  f'AND timestamp>="{start.isoformat()}" AND timestamp<="{end.isoformat()}"',
        "orderBy": "timestamp desc", "pageSize": 40,
    }, token, timeout=20)
    if status != 200:
        raise RuntimeError("RAG 파일별 결과 로그를 조회하지 못했습니다.")
    entries = body.get("entries") or []
    corpora = {str(v).rsplit("/", 1)[-1] for v in (config.get("corpora") or {}).values() if v}
    sinks = {}
    for entry in entries:
        match = re.search(r"sink=(gs://[^\s]+)", str(entry.get("textPayload") or ""))
        if not match:
            continue
        uri = match.group(1)
        parsed = urlparse(uri)
        parts = parsed.path.strip("/").split("/")
        if (parsed.netloc == metadata_bucket and len(parts) == 3 and parts[0] == "import-results"
                and parts[1] in corpora and re.fullmatch(r"[a-f0-9]+\.ndjson", parts[2])):
            sinks.setdefault(uri, entry.get("timestamp", ""))
    if body.get("nextPageToken") or len(sinks) > 20:
        detail["notices"].append("파일별 결과는 해당 작업 시간대의 최근 결과 파일 최대 20개에서 확인했습니다.")

    def read_sink(pair):
        uri, checked_at = pair
        parsed = urlparse(uri)
        url = f"https://storage.googleapis.com/storage/v1/b/{metadata_bucket}/o/{quote(parsed.path.lstrip('/'), safe='')}?alt=media"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read(512 * 1024 + 1)
        if len(payload) > 512 * 1024:
            raise RuntimeError("결과 파일 크기가 조회 한도를 초과했습니다.")
        evidence = []
        for row in parse_import_results(payload):
            source = urlparse(row.gcs_uri)
            if source.netloc != source_bucket or not re.fullmatch(re.escape(file_id) + r"\.[A-Za-z0-9.]+", source.path.lstrip("/")):
                continue
            evidence.append({"object": source.path.lstrip("/"), "status": row.status,
                             "error": str(row.error or "")[:2000], "checkedAt": checked_at,
                             "corpusId": parsed.path.strip("/").split("/")[1]})
        return evidence

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(read_sink, pair) for pair in list(sinks.items())[:20]]
        for future in futures:
            try:
                for row in future.result():
                    key = (row["object"], row["corpusId"])
                    if key not in results or row["checkedAt"] > results[key]["checkedAt"]:
                        results[key] = row
            except (OSError, ValueError, RuntimeError) as exc:
                detail["notices"].append(f"일부 결과 파일을 조회하지 못했습니다: {type(exc).__name__}")
    return list(results.values())


@router.get("/api/v1/departments/{code}/index-issues")
def department_index_issues(code: str, cursor: str = "") -> JSONResponse:
    try:
        config, drives, common, names = _index_issue_context(code)
        result = index_issues.list_issues(core._firestore_client(common["GCP_PROJECT_ID"]), names["docState"], drives,
                                         cursor=cursor, split_collection=names["splitQueue"])
        return JSONResponse({"code": code, "name": config["name"], **result})
    except FileNotFoundError:
        raise HTTPException(404, "학과 설정을 찾을 수 없습니다.") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        return JSONResponse({"error": {"message": f"색인 상태 조회 실패: {str(exc)[:300]}"}}, status_code=503)


@router.get("/api/v1/departments/{code}/index-issues/{file_id}")
def department_index_issue_detail(code: str, file_id: str) -> JSONResponse:
    try:
        config, drives, common, names = _index_issue_context(code)
        detail = index_issues.issue_detail(core._firestore_client(common["GCP_PROJECT_ID"]), names, drives, file_id)
        if not detail.get("resolved"):
            try:
                detail["evidence"] = _index_issue_evidence(file_id, config, common, detail)
            except Exception:
                detail["notices"].append("RAG 파일별 결과를 확인하지 못했습니다. 아래 상태와 관련 작업은 조회된 기록입니다.")
        return JSONResponse(detail)
    except FileNotFoundError:
        raise HTTPException(404, "이 학과의 문서 상태를 찾을 수 없습니다.") from None
    except Exception as exc:
        return JSONResponse({"error": {"message": f"문서 상태 조회 실패: {str(exc)[:300]}"}}, status_code=503)


@router.post("/api/v1/departments/{code}/index-issues/{file_id}/reprocess")
async def reprocess_index_issue(code: str, file_id: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "재처리 요청 형식이 올바르지 않습니다.")
    try:
        run = await asyncio.to_thread(
            sync_history._start_manual_sync, code, "reprocess", file_id=file_id,
            request_id=str(payload.get("requestId") or ""),
        )
        return JSONResponse({"run": run}, status_code=202)
    except FileNotFoundError:
        raise HTTPException(404, "이 학과의 재처리 대상 문서를 찾을 수 없습니다.") from None
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=503)


@router.get("/api/v1/departments/{code}/index-issues/{file_id}/reprocess")
def index_issue_reprocess_history(code: str, file_id: str) -> JSONResponse:
    try:
        config, drives, common, names = _index_issue_context(code)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
            raise FileNotFoundError(file_id)
        snap = core._firestore_client(common["GCP_PROJECT_ID"]).collection(names["docState"]).document(file_id).get(timeout=20)
        data = snap.to_dict() or {}
        if not snap.exists or data.get("driveId") not in drives:
            raise FileNotFoundError(file_id)
        project = common["GCP_PROJECT_ID"]
        region = common["GCP_REGION"]
        history_filter = quote(f'labels."department":"{code}" AND labels."mode":"reprocess"', safe='')
        url = (f"https://workflowexecutions.googleapis.com/v1/projects/{quote(project, safe='')}/locations/"
               f"{quote(region, safe='')}/workflows/{sync_history.SYNC_WORKFLOW_NAME}/executions"
               f"?view=FULL&pageSize=100&orderBy=startTime%20desc&filter={history_filter}")
        status, body, _ = gcp_cli.http_json(url, gcp_cli.access_token(), timeout=20)
        if status != 200:
            raise RuntimeError("재처리 이력을 확인하지 못했습니다. 실행 이력에서 상태를 확인해 주세요.")
        rows = []
        for row in body.get("executions") or []:
            target = sync_history._execution_arguments(row).get("reprocessFile") or {}
            if target.get("fileId") == file_id and target.get("driveId") in drives:
                record = sync_history._sync_execution_record(row, {code: config}, {d: code for d in drives})
                record["result"] = sync_history._json_mapping(row.get("result"))
                rows.append(record)
        return JSONResponse({"runs": rows[:3], "limited": bool(body.get("nextPageToken")),
                             "document": index_issues.issue_item(file_id, data)})
    except FileNotFoundError:
        raise HTTPException(404, "이 학과의 문서를 찾을 수 없습니다.") from None
    except Exception as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=503)
