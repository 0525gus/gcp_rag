"""Compare deployed v1/v2 retrieval evidence without persisting document bodies or keys."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.search_response import response_documents  # noqa: E402


async def collect(url, questions, key, *, v2):
    rows = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=120) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                if v2:
                    assert names == ["search"], names
                for question in questions:
                    started = time.perf_counter()
                    async with asyncio.timeout(120):
                        result = await session.call_tool("search", {
                            "query": question["query"], "top_k": 5,
                        })
                    wire = result.model_dump(mode="json", exclude_none=True)
                    docs = response_documents(wire)
                    ids = [doc["source"]["fileId"] for doc in docs]
                    if v2:
                        payload = result.structuredContent
                        assert payload and payload["schemaVersion"] == 2
                        assert payload["documentCount"] == len(docs) == len(set(ids))
                        assert payload["chunkCount"] == sum(len(d["chunks"]) for d in docs)
                        assert payload["chunkCount"] <= 15
                        assert [d["citationId"] for d in docs] == list(range(1, len(docs) + 1))
                        assert all(d["source"]["sourceUri"] for d in docs)
                        assert "coverage" not in payload and "context" not in payload
                    rows.append({"id": question["id"], "file_ids": ids,
                                 "search_s": round(time.perf_counter() - started, 3),
                                 "response_bytes": len(json.dumps(wire, ensure_ascii=False).encode()),
                                 "chunk_count": (result.structuredContent or {}).get("chunkCount")})
                    print(f"{'v2' if v2 else 'v1'} {question['id']}: {len(docs)} documents", flush=True)
    return {"tools": names, "rows": rows}


async def run(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    key = os.environ["MCP_API_KEY"]
    before = await collect(args.before, questions, key, v2=False)
    after = await collect(args.after, questions, key, v2=True)
    comparisons = [
        {"id": old["id"], "same_ranked_documents": old["file_ids"] == new["file_ids"],
         "same_document_set": set(old["file_ids"]) == set(new["file_ids"])}
        for old, new in zip(before["rows"], after["rows"], strict=True)
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"before": before, "after": after, "comparisons": comparisons,
                                   "note": "Sequential samples; cache/cold state unknown. Not a latency SLA."},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(comparisons), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--questions", type=Path, default=ROOT / "tests/latency_questions.json")
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
