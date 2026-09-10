"""Verify live scope isolation and compare CPU revisions without persisting keys or evidence."""

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dept_gui import _common, _provision_access_token
from scripts.mcp_registry import RegistryAdmin
from shared.search_response import response_documents

logging.disable(logging.CRITICAL)
QUESTIONS = ["장학금 신청 안내", "수강신청 일정", "졸업 요건", "휴학 복학 신청",
             "등록금 납부", "현장실습 신청", "학사 일정", "성적 이의 신청"]


async def connect_search(url, key, queries):
    rows = []
    started = time.perf_counter()
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=120) as client:  # noqa: SIM117
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                init_ms = (time.perf_counter() - started) * 1000
                for query in queries:
                    started = time.perf_counter()
                    result = await session.call_tool("search", {"query": query, "top_k": 5})
                    elapsed = (time.perf_counter() - started) * 1000
                    if result.isError:
                        message = " ".join(getattr(block, "text", "") for block in result.content)
                        raise RuntimeError(message.replace(key, "[redacted]")[:1000])
                    docs = response_documents(result.model_dump(mode="json", exclude_none=True))
                    rows.append({"ms": round(elapsed, 2), "docs": docs})
    return init_ms, rows


def summarize(values):
    ordered = sorted(values)
    return {"n": len(values), "medianMs": round(statistics.median(values), 2),
            "p95Ms": round(ordered[min(len(ordered)-1, int((len(ordered)-1)*.95+.5))], 2),
            "meanMs": round(statistics.mean(values), 2)}


async def run(args):
    common = _common()
    token = _provision_access_token()
    admin = RegistryAdmin(common["GCP_PROJECT_ID"], common["GCP_REGION"], common["FIRESTORE_DATABASE"], token)
    _, registry, configs = admin.read()
    targets = dict(pair.split("=", 1) for pair in args.target)
    db = firestore.Client(project=common["GCP_PROJECT_ID"], database=common["FIRESTORE_DATABASE"],
                          credentials=Credentials(token))
    metadata = {}

    def verify_docs(docs, code, audience):
        for document in docs:
            source = document["source"]
            fid = source["fileId"]
            if fid not in metadata:
                metadata[fid] = db.collection(common.get("DOC_STATE_COLLECTION", "doc_state")).document(fid).get().to_dict()
            meta = metadata[fid]
            assert meta and meta["driveId"] in configs[code]["drive"]["driveIds"], "Department scope leak"
            if audience == "student":
                assert meta.get("audience") == "STUDENT", "Student received staff evidence"
            uri = source.get("sourceUri") or ""
            assert "docs.google.com/" not in uri and "/edit" not in uri, "Editor URI returned"

    report = {"targets": targets, "registryRevision": registry["revision"], "verification": [],
              "timings": {}, "notes": ["End-to-end client timing; includes network and Vertex/Firestore latency.",
                                        "First connection is not a controlled cold-start measurement."]}
    # One URI, four credentials, simultaneous identical questions.
    for label, url in targets.items():
        scopes = [(code, aud, cfg["keys"][aud]) for code, cfg in configs.items()
                  for aud in ("staff", "student") if cfg.get("corpora", {}).get(aud)]
        results = await asyncio.gather(*(connect_search(url, key, QUESTIONS[:2]) for _, _, key in scopes))
        for (code, audience, _), (init, rows) in zip(scopes, results):
            for row in rows:
                verify_docs(row["docs"], code, audience)
            report["verification"].append({"target": label, "department": code, "audience": audience,
                                            "initMs": round(init, 2), "documents": sum(len(r["docs"]) for r in rows)})
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers={"Authorization": "Bearer invalid-credential"}, json={})
            assert response.status_code == 401, "Invalid key was accepted"
            response = await client.post(url, json={})
            assert response.status_code == 401, "Missing key was accepted"
        print(f"{label}: {len(scopes)} scopes verified; invalid/missing keys rejected", flush=True)

    if args.benchmark:
        key = configs["cs"]["keys"]["staff"]
        timings = defaultdict(list)
        # Alternate revision order each round to reduce backend warming/order bias.
        names = list(targets)
        for round_index in range(3):
            for label in names if round_index % 2 == 0 else names[::-1]:
                _, rows = await connect_search(targets[label], key, QUESTIONS)
                timings[label].extend(row["ms"] for row in rows)
                print(f"{label}: sequential round {round_index+1} complete", flush=True)
        for label, url in targets.items():
            burst = await asyncio.gather(*(connect_search(url, key, QUESTIONS[i:i+2]) for i in range(4)))
            parallel = [row["ms"] for _, rows in burst for row in rows]
            report["timings"][label] = {"sequential": summarize(timings[label]),
                                         "concurrent4": summarize(parallel),
                                         "sequentialSamplesMs": timings[label], "concurrentSamplesMs": parallel}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verification": report["verification"], "timings": {
        label: {k:v for k,v in value.items() if "Samples" not in k} for label,value in report["timings"].items()
    }}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", required=True, help="label=https://service/mcp")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
