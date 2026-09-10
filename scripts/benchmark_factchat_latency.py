"""Measure FactChat gateway latency without saving credentials or answer bodies."""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "https://factchat-cloud.mindlogic.ai/v1/gateway/chatbots/51767/chat/completions"


async def run(args):
    key = os.environ["FACTCHAT_API_KEY"]
    questions = json.loads((ROOT / "tests/latency_questions.json").read_text(encoding="utf-8"))
    rows = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for question in questions:
        if question["id"] == "Q5":
            continue
        row = {**question, "started_utc": datetime.now(timezone.utc).isoformat(), "stream": args.stream}
        started = time.perf_counter()
        contents = []
        events = 0
        done = False
        try:
            async with asyncio.timeout(120):
                async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
                    async with client.stream("POST", URL,
                                             headers={"Authorization": f"Bearer {key}"},
                                             json={"messages": [{"role": "user", "content": question["query"]}],
                                                   "stream": args.stream}) as response:
                        row["headers_s"] = round(time.perf_counter() - started, 3)
                        row["http_status"] = response.status_code
                        row["content_type"] = response.headers.get("content-type")
                        if response.status_code != 200:
                            row["status"] = "http_error"
                            # Diagnostic error body is intentionally not persisted.
                            error = (await response.aread()).decode(errors="replace")
                            print("HTTP error:", error.replace(key, "[REDACTED]")[:600], flush=True)
                        elif "text/event-stream" in response.headers.get("content-type", ""):
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                if "first_event_s" not in row:
                                    row["first_event_s"] = round(time.perf_counter() - started, 3)
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    done = True
                                    row["completion_s"] = round(time.perf_counter() - started, 3)
                                    continue
                                item = json.loads(data)
                                events += 1
                                if "error" in item:
                                    row["status"] = "stream_error"
                                if item.get("usage"):
                                    row["usage"] = item["usage"]
                                if item.get("model"):
                                    row["model"] = item["model"]
                                for choice in item.get("choices", []):
                                    text = choice.get("delta", {}).get("content")
                                    if isinstance(text, str) and text:
                                        row.setdefault("first_content_s", round(time.perf_counter() - started, 3))
                                        contents.append(text)
                                    if choice.get("finish_reason"):
                                        row["finish_reason"] = choice["finish_reason"]
                            row.setdefault("status", "ok" if done or row.get("finish_reason") else "incomplete")
                        else:
                            item = json.loads(await response.aread())
                            row["completion_s"] = round(time.perf_counter() - started, 3)
                            row["response_keys"] = list(item)
                            row["usage"] = item.get("usage")
                            row["model"] = item.get("model")
                            for choice in item.get("choices", []):
                                text = choice.get("message", {}).get("content")
                                if isinstance(text, str):
                                    contents.append(text)
                                row["finish_reason"] = choice.get("finish_reason")
                            row["status"] = "ok" if contents else "unexpected_response"
        except Exception as exc:
            row["status"] = "error"
            row["error_type"] = type(exc).__name__
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        row["ended_utc"] = datetime.now(timezone.utc).isoformat()
        row["answer_chars"] = sum(map(len, contents))
        row["events"] = events
        rows.append(row)
        args.out.write_text(json.dumps({"url": URL, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{row['id']} {row['status']} first_content={row.get('first_content_s')}s "
              f"complete={row.get('completion_s', row['elapsed_s'])}s chars={row['answer_chars']}", flush=True)
        if row["status"] != "ok":
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
