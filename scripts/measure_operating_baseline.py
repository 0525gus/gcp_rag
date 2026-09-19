"""Measure the deployed MCP ``search`` baseline against frozen Golden200.

This intentionally exercises the normal MCP search path, not FactChat and not
the per-channel diagnostic routes.  Each case has one request:

* ``top_k=20``: the current deployment returns at most 15 context documents;
  record the gold rank within that observable list and Hit@1/3/5/10/15.

Candidate Recall@20/@30 remains explicitly ``not_exposed``.  A 15-document
context response must not be relabelled as a 20- or 30-document candidate pool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dept_config import build_env
from scripts.eval_golden import call_search_result, evidence_matches, evidence_recall
from scripts.eval_retrieval_channels import DEFAULT_SNAPSHOT, SPLIT_FILES, load_snapshot

OPERATIONAL_REQUEST_TOP_K = 20
OBSERVABLE_RANK_DEPTH = 15
CANDIDATE_KS = (20, 30)
DEFAULT_SET = "dev150"
TARGET_SETS = {
    "dev150": "dev",
    "holdout50": "holdout",
    "golden200": "all",
}
WEAKNESS_TAGS = (
    "period",
    "sequence",
    "revision",
    "numeric_table",
    "similar_title",
    "multi_condition",
    "distributed_evidence",
)
TAG_ALIASES = {
    "period_conflict": "period",
    "sequence_conflict": "sequence",
    "revision_conflict": "revision",
    "similar_title_bundle": "similar_title",
}


def resolve_target_set(target_set: str) -> str:
    """Resolve the public set name and keep final-validation data sealed."""
    if target_set not in TARGET_SETS:
        raise ValueError(f"unknown target set: {target_set}")
    if target_set != DEFAULT_SET and not os.environ.get("ALLOW_HOLDOUT_RUN"):
        raise RuntimeError("holdout50은 STEP 7 최종 검증 전용")
    return TARGET_SETS[target_set]


def _registry_scope(department: str, audience: str) -> tuple[str, str]:
    """Read the deployed scoped key via the same gcloud identity used by Phase-1."""
    from scripts.dept_gui import _mcp_registry_read

    record, configs = _mcp_registry_read()
    config = configs.get(department) or {}
    key = str((config.get("keys") or {}).get(audience) or "").strip()
    service_url = str(record.get("serviceUrl") or "").strip().rstrip("/")
    if not key:
        raise RuntimeError(f"registry has no scoped MCP key for {department}/{audience}")
    if not service_url.startswith("https://"):
        raise RuntimeError("registry has no valid MCP service URL")
    return f"{service_url}/mcp", key


def _index_identity(department: str, audience: str) -> tuple[int, str]:
    """Snapshot the scoped corpus file inventory once before the baseline run."""
    from scripts.run_phase1_diagnosis import (
        _gcloud_access_credentials,
        _rag_file_counts,
        _settings_from_registry,
    )

    _credentials, access_token = _gcloud_access_credentials()
    settings, _drive_ids = _settings_from_registry(department, audience)
    counts = _rag_file_counts(access_token, settings)
    payload = "\n".join(f"{file_id}|{count}" for file_id, count in sorted(counts.items()))
    return len(counts), "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_identity() -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return commit, dirty


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rank(documents: list[dict[str, Any]], accepted: set[str]) -> int | None:
    return next(
        (
            position
            for position, document in enumerate(documents, 1)
            if str((document.get("source") or {}).get("fileId") or "") in accepted
        ),
        None,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    return sorted(values)[round((len(values) - 1) * percentile)]


def _tag_set(golden: dict[str, Any]) -> set[str]:
    return {
        TAG_ALIASES.get(str(tag), str(tag))
        for tag in golden.get("weakness_tags", [])
    }


def _request(
    url: str, key: str, query: str, top_k: int, retries: int
) -> tuple[list[dict[str, Any]], dict[str, Any], float, str]:
    started = time.perf_counter()
    error = ""
    documents: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    for attempt in range(retries):
        try:
            response = call_search_result(url, key, query, top_k)
            documents = response["documents"]
            diagnostics = response.get("retrievalDiagnostics") or {}
            error = ""
            break
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
    return documents, diagnostics, round((time.perf_counter() - started) * 1000, 2), error


def _metrics(rows: list[dict[str, Any]], *, candidate_depth: int | None = None) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    prefix = f"candidate_{candidate_depth}" if candidate_depth else "operational"
    ranks = [row[f"{prefix}_rank"] for row in rows]
    evidence_found = sum(int(row["operational_evidence_found"]) for row in rows)
    evidence_total = sum(int(row["operational_evidence_total"]) for row in rows)
    result: dict[str, Any] = {"n": n}
    if candidate_depth:
        errors = sum(bool(row[f"candidate_{candidate_depth}_error"]) for row in rows)
        exposed = all(bool(row[f"candidate_{candidate_depth}_depth_exposed"]) for row in rows)
        result[f"candidate_recall@{candidate_depth}_errors"] = errors
        result[f"candidate_recall@{candidate_depth}_status"] = (
            "measured" if not errors and exposed else "not_exposed_by_operational_response"
        )
        if errors or not exposed:
            result[f"candidate_recall@{candidate_depth}"] = None
            result[f"candidate_recall@{candidate_depth}_hits"] = None
            return result
        result[f"candidate_recall@{candidate_depth}"] = round(
            sum(bool(rank) for rank in ranks) / n, 4
        )
        result[f"candidate_recall@{candidate_depth}_hits"] = sum(bool(rank) for rank in ranks)
        return result

    for cutoff in (1, 3, 5, 10, 15):
        hits = sum(bool(rank and rank <= cutoff) for rank in ranks)
        result[f"hit@{cutoff}"] = round(hits / n, 4)
        result[f"hit@{cutoff}_hits"] = hits
    result["mrr"] = round(sum(1 / rank if rank else 0 for rank in ranks) / n, 4)
    result["evidence_recall@context"] = (
        round(evidence_found / evidence_total, 4) if evidence_total else None
    )
    result["evidence_found"] = evidence_found
    result["evidence_total"] = evidence_total
    latencies = [float(row["operational_latency_ms"]) for row in rows]
    result["latency_median_ms"] = round(statistics.median(latencies), 2)
    result["latency_p95_ms"] = round(_percentile(latencies, 0.95) or 0, 2)
    result["errors"] = sum(bool(row["operational_error"]) for row in rows)
    return result


def _summary_rows(rows: list[dict[str, Any]], top30_exposed: bool) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    groups: list[tuple[str, list[dict[str, Any]]]] = [("overall", rows)]
    groups.extend(
        (tag, [row for row in rows if tag in json.loads(row["weakness_tags"])])
        for tag in WEAKNESS_TAGS
    )
    for scope, group in groups:
        base = {"scope": scope, **_metrics(group)}
        base.update(_metrics(group, candidate_depth=20))
        base.update(_metrics(group, candidate_depth=30))
        if not top30_exposed:
            base["candidate_recall@30"] = None
            base["candidate_recall@30_hits"] = None
            base["candidate_recall@30_status"] = "not_exposed_by_operational_response"
        output.append(base)
    return output


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(
    golden: list[dict[str, Any]],
    *,
    url: str,
    key: str,
    retries: int,
    common: dict[str, Any],
    request_top_k: int = OPERATIONAL_REQUEST_TOP_K,
    context_file: TextIO | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    for position, golden_row in enumerate(golden, 1):
        accepted = {*golden_row["expected_file"], *golden_row.get("also_accept", [])}
        request_result = _request(
            url, key, golden_row["query"], request_top_k, retries
        )
        if len(request_result) == 3:  # Compatibility with existing test doubles.
            documents, latency, error = request_result
            diagnostics: dict[str, Any] = {}
        else:
            documents, diagnostics, latency, error = request_result
        observable_rank_depth = min(OBSERVABLE_RANK_DEPTH, request_top_k)
        documents = documents[:observable_rank_depth]
        if context_file is not None:
            context_file.write(json.dumps({
                "query_id": golden_row["n"],
                "query": golden_row["query"],
                "request_top_k": request_top_k,
                "raw_hits": diagnostics.get("rawHitCount"),
                "raw_vertex_hits": diagnostics.get("rawVertexHitCount"),
                "rewrite_vertex_hits": diagnostics.get("rewriteVertexHitCount"),
                "latency_ms": latency,
                "documents": documents,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            context_file.flush()
        rank_at_15 = _rank(documents, accepted)
        evidence_found, evidence_total = evidence_recall(golden_row["expected_evidence"], documents)
        fact_found = evidence_matches(golden_row["expected_evidence"], documents)
        row: dict[str, Any] = {
            **common,
            "case_n": golden_row["n"],
            "query": golden_row["query"],
            "weakness_tags": json.dumps(sorted(_tag_set(golden_row)), ensure_ascii=False),
            "expected_file": json.dumps(sorted(accepted)),
            "operational_requested_top_k": request_top_k,
            "observable_rank_depth": observable_rank_depth,
            "operational_returned_documents": len(documents),
            "rank_at_15": rank_at_15,
            "operational_rank": rank_at_15,
            "operational_evidence_found": evidence_found,
            "operational_evidence_total": evidence_total,
            "fact_found": json.dumps(fact_found),
            "operational_latency_ms": latency,
            "operational_error": error,
            "raw_hits": diagnostics.get("rawHitCount"),
            "raw_vertex_hits": diagnostics.get("rawVertexHitCount"),
            "rewrite_vertex_hits": diagnostics.get("rewriteVertexHitCount"),
            "returned_chunks": sum(len(document.get("chunks", [])) for document in documents),
            "chunks_per_document": (
                round(
                    sum(len(document.get("chunks", [])) for document in documents) / len(documents),
                    6,
                )
                if documents
                else None
            ),
        }
        for candidate_k in CANDIDATE_KS:
            row.update(
                {
                    f"candidate_{candidate_k}_requested_top_k": candidate_k,
                    f"candidate_{candidate_k}_returned_documents": None,
                    f"candidate_{candidate_k}_candidate_count": None,
                    f"candidate_{candidate_k}_depth_exposed": False,
                    f"candidate_{candidate_k}_rank": None,
                    f"candidate_{candidate_k}_latency_ms": None,
                    f"candidate_{candidate_k}_error": "",
                    f"candidate_{candidate_k}_ranked_file_ids": "[]",
                }
            )
        rows.append(row)
        print(f"{position}/{len(golden)} case={golden_row['n']}", file=sys.stderr, flush=True)
    return rows, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--target-set", choices=tuple(TARGET_SETS), default=DEFAULT_SET)
    parser.add_argument("--mcp-url", default=os.environ.get("MCP_URL", ""))
    parser.add_argument("--dept", help="load the scoped MCP key from the Cloud registry")
    parser.add_argument(
        "--registry-dept",
        help="read the scoped MCP key and URL from the Cloud registry using gcloud auth",
    )
    parser.add_argument("--audience", choices=("staff", "student"), default="staff")
    parser.add_argument("--api-key-env", default="MCP_API_KEY")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=OPERATIONAL_REQUEST_TOP_K)
    parser.add_argument("--limit", type=int, default=0, help="run only the first N dev cases")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    if args.retries < 1:
        parser.error("--retries must be at least 1")
    if not 1 <= args.top_k <= 20:
        parser.error("--top-k must be between 1 and 20")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    try:
        split = resolve_target_set(args.target_set)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    url = args.mcp_url.strip()
    index_document_count: int | None = None
    index_version = ""
    if args.registry_dept:
        try:
            registry_url, key = _registry_scope(args.registry_dept, args.audience)
            index_document_count, index_version = _index_identity(
                args.registry_dept, args.audience
            )
        except RuntimeError as exc:
            parser.error(str(exc))
        url = url or registry_url
    else:
        key = (
            build_env(args.dept, args.audience)["MCP_API_KEY"]
            if args.dept
            else os.environ.get(args.api_key_env, "").strip()
        )
    if not url:
        parser.error("--mcp-url, MCP_URL, or --registry-dept is required")
    if not key:
        parser.error("a scoped MCP key is required (--registry-dept, --dept, or --api-key-env)")
    manifest, golden = load_snapshot(args.snapshot_dir, split)
    if args.limit:
        golden = golden[: args.limit]
    commit, dirty = _git_identity()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or ROOT / "tests" / "_bench_out" / "operating_baseline" / run_id
    snapshot_file = SPLIT_FILES[split]
    execution_started = datetime.now(UTC)
    common = {
        "run_id": run_id,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_sha256": _sha256(args.snapshot_dir / snapshot_file),
        "target_set": args.target_set,
        "split": split,
        "mcp_url": url.rstrip("/"),
        "code_commit": commit,
        "code_dirty": dirty,
        "index_document_count": index_document_count,
        "index_version": index_version,
        "started_utc": execution_started.isoformat(),
    }
    run_started = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "context_capture.jsonl").open("w", encoding="utf-8") as context_file:
        rows, top30_exposed = run(
            golden,
            url=url,
            key=key,
            retries=args.retries,
            common=common,
            request_top_k=args.top_k,
            context_file=context_file,
        )
    duration_seconds = round(time.perf_counter() - run_started, 3)
    summary_rows = _summary_rows(rows, top30_exposed)
    _write_csv(out_dir / "baseline_rows.csv", rows)
    _write_csv(out_dir / "baseline_summary.csv", summary_rows)
    metadata = {
        **common,
        "cases": len(rows),
        "operational_requested_top_k": args.top_k,
        "candidate_recall_definition": (
            "not measured: active search response exposes at most rank 15"
        ),
        "candidate_route": None,
        "candidate_20_requested": 20,
        "candidate_30_requested": 30,
        "candidate_30_exposed": top30_exposed,
        "candidate_30_max_returned_documents": max(
            (int(row["candidate_30_returned_documents"] or 0) for row in rows), default=0
        ),
        "execution_duration_seconds": duration_seconds,
        "ended_utc": datetime.now(UTC).isoformat(),
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary_rows[0], ensure_ascii=False, indent=2))
    print(f"baseline rows: {out_dir / 'baseline_rows.csv'}")
    print(f"baseline summary: {out_dir / 'baseline_summary.csv'}")
    has_error = any(
        row["operational_error"]
        or row["candidate_20_error"]
        or row["candidate_30_error"]
        for row in rows
    )
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
