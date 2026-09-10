"""Bounded production MCP search load test; never persist keys or evidence."""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from google.cloud import firestore
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dept_gui import _common, _provision_access_token
from scripts.mcp_registry import RegistryAdmin
from shared.search_response import response_documents

QUESTIONS = ["장학금 신청 안내", "수강신청 일정", "졸업 요건", "휴학 복학 신청",
             "등록금 납부", "현장실습 신청", "학사 일정", "성적 이의 신청"]


def stats(values):
    if not values:
        return {}
    ordered = sorted(values)
    return {"medianMs": round(statistics.median(values), 2),
            "p95Ms": round(ordered[math.ceil(len(values) * .95) - 1], 2),
            "maxMs": round(max(values), 2), "meanMs": round(statistics.mean(values), 2)}


def rpc_body(response):
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                body = json.loads(line[5:])
                if "result" in body or "error" in body:
                    return body
        raise ValueError("No RPC result")
    return response.json()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 10, 20, 40])
    parser.add_argument("--requests-per-worker", type=int, default=10)
    args = parser.parse_args()
    if any(not 1 <= n <= 40 for n in args.concurrency) or not 1 <= args.requests_per_worker <= 10:
        parser.error("Use 1..40 concurrent workers and 1..10 requests per worker")
    common = _common()
    token = _provision_access_token()
    admin = RegistryAdmin(common["GCP_PROJECT_ID"], common["GCP_REGION"],
                          common["FIRESTORE_DATABASE"], token)
    _, record, configs = admin.read()
    url = record["serviceUrl"].rstrip("/") + "/mcp"
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "docs/verification" / f"unified-load-{run_id}.json"
    report = {"runId": run_id, "url": url, "startedAt": datetime.now(UTC).isoformat(),
              "userAgent": f"gcp-rag-loadtest/{run_id}", "stages": [],
              "method": f"Closed-loop concurrent workers, {args.requests_per_worker} searches each; alternating CS staff/student. "
                        "Unique synthetic query suffix avoids exact-query cache hits. "
                        "HTTP JSON-RPC tools/call on stateless MCP; pooled connections; 30s timeout. "
                        "Document scope checked after timed stages. Not a single-instance or cold-start benchmark."}
    db = firestore.Client(project=common["GCP_PROJECT_ID"], database=common["FIRESTORE_DATABASE"],
                          credentials=Credentials(token))
    seen = set()
    request_index = 0
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=80,
                                 max_keepalive_connections=80),
                                 headers={"Accept": "application/json, text/event-stream",
                                          "User-Agent": report["userAgent"]}) as client:
        for concurrency in args.concurrency:
            rows = []
            started = time.perf_counter()
            stage_start = datetime.now(UTC).isoformat()

            async def worker(worker_id, rows=rows):
                nonlocal request_index
                audience = "student" if worker_id % 2 else "staff"
                for _ in range(args.requests_per_worker):
                    request_index += 1
                    index = request_index
                    query = QUESTIONS[index % len(QUESTIONS)] + f" (부하검증 요청 {run_id}-{index})"
                    tick = time.perf_counter()
                    error = ""
                    status = None
                    count = 0
                    try:
                        response = await client.post(url,
                            headers={"Authorization": f"Bearer {configs['cs']['keys'][audience]}"},
                            json={"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                  "params": {"name": "search", "arguments": {"query": query, "top_k": 5}}})
                        status = response.status_code
                        if status != 200:
                            error = f"http_{status}"
                        else:
                            body = rpc_body(response)
                            result = body.get("result", {})
                            if body.get("error") or result.get("isError"):
                                message = json.dumps(body).lower()
                                error = "quota_or_rate_limit" if any(t in message for t in
                                    ("429", "resource_exhausted", "quota")) else "mcp_error"
                            else:
                                docs = response_documents(result)
                                count = len(docs)
                                for doc in docs:
                                    seen.add((audience, doc["source"]["fileId"]))
                    except httpx.TimeoutException:
                        error = "timeout"
                    except (httpx.HTTPError, ValueError, KeyError):
                        error = "transport_or_response_error"
                    rows.append({"ms": round((time.perf_counter() - tick) * 1000, 2),
                                 "status": status, "error": error, "documents": count})
                    if len([r for r in rows if r["error"]]) >= 5:
                        break

            await asyncio.gather(*(worker(i) for i in range(concurrency)))
            elapsed = time.perf_counter() - started
            errors = Counter(row["error"] for row in rows if row["error"])
            stage = {"concurrency": concurrency, "startedAt": stage_start,
                     "finishedAt": datetime.now(UTC).isoformat(), "requests": len(rows),
                     "errors": dict(errors), "errorCount": sum(errors.values()),
                     "elapsedSeconds": round(elapsed, 2), "requestsPerSecond": round(len(rows)/elapsed, 2),
                     "successfulLatency": stats([r["ms"] for r in rows if not r["error"]]),
                     "allLatency": stats([r["ms"] for r in rows]), "samples": rows}
            report["stages"].append(stage)
            report["finishedAt"] = datetime.now(UTC).isoformat()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in stage.items() if k != "samples"}), flush=True)
            if errors:
                report["stoppedEarly"] = "Errors observed; higher load skipped"
                break
    violations = 0
    metadata = {}
    for audience, file_id in seen:
        if file_id not in metadata:
            metadata[file_id] = db.collection(common.get("DOC_STATE_COLLECTION", "doc_state")).document(file_id).get().to_dict()
        meta = metadata[file_id]
        if not meta or meta.get("driveId") not in configs["cs"]["drive"]["driveIds"] or audience == "student" and meta.get("audience") != "STUDENT":
            violations += 1
    report["scopeAudit"] = {"uniqueFiles": len(metadata), "fileAudiencePairs": len(seen),
                            "violations": violations}
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(out), "scopeAudit": report["scopeAudit"]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
