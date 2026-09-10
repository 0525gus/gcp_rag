"""Measure sequential MCP searches; no retries, no answer generation.

Use MCP_API_KEY from the environment, never from command-line arguments.
Each question uses a fresh session; initialization and search are timed separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.search_response import response_documents  # noqa: E402


async def measure(args: argparse.Namespace) -> None:
    key = os.environ.get("MCP_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set MCP_API_KEY in the process environment.")
    endpoint = urlsplit(args.url)
    if endpoint.scheme != "https" or endpoint.username or endpoint.password:
        raise SystemExit("Use an HTTPS MCP URL without embedded credentials.")
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    report = {"url": args.url, "top_k": args.top_k, "timeout_s": args.timeout,
              "cache_state": "unknown; identical recent queries may be cached", "rows": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for question in questions:
        row = {**question, "started_utc": datetime.now(timezone.utc).isoformat()}
        started = time.perf_counter()
        phase = "initialize"
        try:
            async with asyncio.timeout(args.timeout):
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=httpx.Timeout(args.timeout), follow_redirects=False,
                ) as client:
                    async with streamable_http_client(args.url, http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            row["initialize_s"] = round(time.perf_counter() - started, 3)
                            phase = "search"
                            search_started = time.perf_counter()
                            result = await session.call_tool("search", {
                                "query": question["query"], "top_k": args.top_k,
                            })
                            row["search_s"] = round(time.perf_counter() - search_started, 3)
                            row["response_complete_s"] = round(time.perf_counter() - started, 3)
                            row["status"] = "tool_error" if result.isError else "ok"
                            row["response_chars"] = sum(
                                len(block.text) for block in result.content if block.type == "text"
                            )
                            wire_result = result.model_dump(mode="json", exclude_none=True)
                            row["response_bytes"] = len(json.dumps(wire_result, ensure_ascii=False).encode("utf-8"))
                            if not result.isError:
                                documents = response_documents(wire_result)
                                row["document_count"] = len(documents)
                                structured = result.structuredContent or {}
                                row["chunk_count"] = structured.get("chunkCount")
                            phase = "close"
        except Exception as exc:
            # Do not persist response bodies, authorization headers, or sensitive error text.
            row["status"] = "error"
            row["error_type"] = type(exc).__name__
            row["error_phase"] = phase
        row["elapsed_including_close_s"] = round(time.perf_counter() - started, 3)
        report["rows"].append(row)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{row['id']}: {row['status']} search={row.get('search_s', '-')}s "
              f"total={row['elapsed_including_close_s']}s", flush=True)
        if row["status"] == "error":
            break  # Do not repeat a broken connection six times.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--questions", type=Path, default=ROOT / "tests/latency_questions.json")
    parser.add_argument("--out", type=Path, default=ROOT / "latency-results.json")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0 or not 1 <= args.top_k <= 20:
        parser.error("timeout must be positive; top-k must be 1..20")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
