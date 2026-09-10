"""Test actual FactChat conversations; require a verified unified-MCP canary first."""

import argparse
import asyncio
import getpass
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dept_gui import _common, _provision_access_token


def summary(rows, field):
    values = sorted(r[field] for r in rows if r.get("status") == "ok" and field in r)
    return {} if not values else {"n": len(values), "medianSeconds": round(statistics.median(values), 3),
                                  "p95Seconds": values[math.ceil(len(values)*.95)-1], "maxSeconds": max(values)}


def backend_audit(marker):
    common = _common()
    with httpx.Client(headers={"Authorization": f"Bearer {_provision_access_token()}"}, timeout=30) as client:
        payload = {
            "resourceNames": [f"projects/{common['GCP_PROJECT_ID']}"], "pageSize": 1000,
            "filter": f'resource.type="cloud_run_revision" AND resource.labels.service_name:"rag-mcp" AND '
                      f'timestamp>="{(datetime.now(UTC)-timedelta(minutes=15)).isoformat()}" AND '
                      f'(textPayload:"{marker}" OR jsonPayload.message:"{marker}")'}
        entries = []
        while True:
            response = client.post("https://logging.googleapis.com/v2/entries:list", json=payload)
            response.raise_for_status()
            body = response.json()
            entries.extend(body.get("entries", []))
            if not body.get("nextPageToken"):
                break
            payload["pageToken"] = body["nextPageToken"]
    matched = []
    for e in entries:
        message = e.get("textPayload") or e.get("jsonPayload", {}).get("message", "")
        if "search query=" in message and marker in message:
            labels = e["resource"]["labels"]
            matched.append({"service": labels.get("service_name"), "revision": labels.get("revision_name"),
                            "timestamp": e["timestamp"],
                            "markers": re.findall(r"FCLOAD\d{8}T\d{6}ZR\d{2}", message)})
    return matched


async def run(args):
    key = (getpass.getpass("FactChat API key: ").strip() if args.key_prompt else
           args.key_file.read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("FACTCHAT_API_KEY", ""))
    if not key:
        raise SystemExit("Set FACTCHAT_API_KEY or supply --key-file (plain text); never pass the key on the command line")
    urls = {"staff": f"https://factchat-cloud.mindlogic.ai/v1/gateway/chatbots/{args.chatbot_id}/chat/completions",
            "student": f"https://factchat-cloud.mindlogic.ai/v1/gateway/chatbots/{args.student_chatbot_id}/chat/completions"}
    run_id = "FCLOAD" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    questions = json.loads((ROOT / "tests/latency_questions.json").read_text(encoding="utf-8"))
    report = {"runId": run_id, "urls": urls, "audience": args.audience, "expectedRevision": args.revision,
              "startedAt": datetime.now(UTC).isoformat(), "requestedConcurrency": 40,
              "method": "40 simultaneous independent FactChat streaming conversations. Prompt explicitly requests MCP search with a unique marker. No automatic retries. Not 40 synchronized MCP tool invocations.",
              "rows": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async with httpx.AsyncClient(timeout=120, limits=httpx.Limits(max_connections=45,
                                 max_keepalive_connections=45), follow_redirects=False) as client:
        async def conversation(index):
            audience = ("student" if index % 2 else "staff") if args.audience == "mixed" else args.audience
            url = urls[audience]
            marker = f"{run_id}R{index:02d}"
            question = questions[index % len(questions)]["query"]
            prompt = (question + "\n연결된 MCP search 도구로 자료를 조회한 뒤 근거와 함께 답변해 주세요. "
                      f"검증을 위해 도구의 검색어 끝에 식별자 {marker}를 그대로 포함하고, 답변에는 식별자를 생략해 주세요.")
            row = {"index": index, "audience": audience, "marker": marker, "startedAt": datetime.now(UTC).isoformat()}
            tick = time.perf_counter()
            chars = 0
            finished = False
            try:
                async with asyncio.timeout(120), client.stream("POST", url,
                    headers={"Authorization": f"Bearer {key}"},
                    json={"messages": [{"role": "user", "content": prompt}], "stream": True}) as response:
                    row["httpStatus"] = response.status_code
                    if response.status_code != 200:
                        row["status"] = "http_error"
                    elif "text/event-stream" not in response.headers.get("content-type", ""):
                        row["status"] = "unexpected_nonstream_response"
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                finished = True
                                continue
                            event = json.loads(data)
                            if event.get("error"):
                                row["status"] = "stream_error"
                            for choice in event.get("choices", []):
                                text = choice.get("delta", {}).get("content")
                                if isinstance(text, str) and text:
                                    row.setdefault("firstContentSeconds", round(time.perf_counter()-tick, 3))
                                    chars += len(text)
                                if choice.get("finish_reason"):
                                    finished = True
                        row.setdefault("status", "ok" if finished and chars else "incomplete")
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                row.update(status="error", errorType=type(exc).__name__)
            row.update(completionSeconds=round(time.perf_counter()-tick, 3), answerChars=chars,
                       finishedAt=datetime.now(UTC).isoformat())
            return row

        report["canaries"] = []
        for index in ((0, 41) if args.audience == "mixed" else (0,)):
            canary = await conversation(index)
            report["canaries"].append(canary)
            save()
            if canary["status"] != "ok":
                report["blocked"] = "FactChat canary failed; 40-way test not sent"
                save()
                print(json.dumps({"blocked": report["blocked"], "status": canary["status"], "httpStatus": canary.get("httpStatus")}))
                return
            for attempt in range(3):
                matches = await asyncio.to_thread(backend_audit, canary["marker"])
                if matches:
                    break
                if attempt < 2:
                    await asyncio.sleep(10)
            canary["backend"] = matches
            if not matches or any(m["service"] != "rag-mcp" or m["revision"] != args.revision for m in matches):
                report["blocked"] = "Cannot verify FactChat search on the intended 2-vCPU revision; 40-way test not sent"
                save()
                print(json.dumps({"blocked": report["blocked"], "backend": matches}))
                return
        print("FactChat canary reached the intended revision; sending 40 conversations", flush=True)
        tasks = [asyncio.create_task(conversation(i)) for i in range(1, 41)]
        for task in asyncio.as_completed(tasks):
            row = await task
            report["rows"].append(row)
            save()
            print(f"completed={len(report['rows'])}/40 status={row['status']} elapsed={row['completionSeconds']}s", flush=True)
    report["finishedAt"] = datetime.now(UTC).isoformat()
    report["summary"] = {"statuses": dict(Counter(r["status"] for r in report["rows"])),
                         "firstContent": summary(report["rows"], "firstContentSeconds"),
                         "completion": summary(report["rows"], "completionSeconds")}
    report["backend"] = await asyncio.to_thread(backend_audit, run_id)
    save()
    print(json.dumps(report["summary"]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--key-prompt", action="store_true")
    parser.add_argument("--chatbot-id", type=int, default=51767)
    parser.add_argument("--student-chatbot-id", type=int, default=51766)
    parser.add_argument("--audience", choices=("mixed", "staff", "student"), default="mixed")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
