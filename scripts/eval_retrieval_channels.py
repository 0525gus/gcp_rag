"""Evaluate vector/body/title/bundle/metadata paths on the frozen Golden200 split.

The output contract is deliberately flat: one row per query, with each channel's
rank, latency, evidence recall, and ranked IDs stored in prefixed scalar columns.

Examples:
    $env:MCP_URL = "https://<rag-mcp-service>/mcp"
    python scripts/eval_retrieval_channels.py --dept cs --split dev
    python scripts/eval_retrieval_channels.py --dept cs --split holdout --top-k 20

This calls the RAG MCP service directly.  It does not call FactChat.
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
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dept_config import build_env
from scripts.eval_golden import evidence_recall, normalize_golden
from shared.retrieval_channels import RETRIEVAL_CHANNELS

DEFAULT_SNAPSHOT = ROOT / "tests" / "frozen" / "golden200-2026-09-13"
SPLIT_FILES = {
    "all": "golden200.json",
    "dev": "golden200_dev150.json",
    "holdout": "golden200_holdout50.json",
}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_snapshot(snapshot_dir: Path, split: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename = SPLIT_FILES[split]
    data_path = snapshot_dir / filename
    expected_hash = (manifest.get("files") or {}).get(filename)
    actual_hash = _sha256(data_path)
    if not expected_hash or actual_hash != expected_hash:
        raise ValueError(
            f"frozen snapshot hash mismatch for {filename}: "
            f"manifest={expected_hash!r}, actual={actual_hash!r}"
        )
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    rows = [normalize_golden(item, index) for index, item in enumerate(raw, 1)]
    expected_count = manifest.get(f"{split}_cases") if split != "all" else manifest.get("all_cases")
    if expected_count != len(rows):
        raise ValueError(f"snapshot count mismatch: manifest={expected_count}, actual={len(rows)}")
    return manifest, rows


def git_identity(root: Path = ROOT) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return commit, dirty


def retrieval_base_url(explicit: str | None = None) -> str:
    value = (
        explicit or os.environ.get("RETRIEVAL_BASE_URL") or os.environ.get("MCP_URL") or ""
    ).strip()
    if not value:
        raise ValueError("--base-url, RETRIEVAL_BASE_URL, or MCP_URL is required")
    value = value.rstrip("/")
    return value.removesuffix("/mcp")


def call_channel(
    base_url: str,
    key: str,
    channel: str,
    query: str,
    top_k: int,
    candidate_k: int,
    *,
    timeout: float = 120,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}/retrieval/{channel}",
        data=json.dumps(
            {"query": query, "top_k": top_k, "candidate_k": candidate_k},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _expected_rank(results: list[dict[str, Any]], expected: set[str]) -> int | None:
    return next(
        (
            index
            for index, result in enumerate(results, 1)
            if (result.get("source") or {}).get("fileId") in expected
        ),
        None,
    )


def _bundle_rank(results: list[dict[str, Any]], expected: set[str]) -> int | None:
    return next(
        (
            index
            for index, result in enumerate(results, 1)
            if str((result.get("source") or {}).get("bundle") or "") in expected
        ),
        None,
    )


def add_channel_columns(
    row: dict[str, Any],
    channel: str,
    payload: dict[str, Any],
    golden: dict[str, Any],
    wall_latency_ms: float,
) -> None:
    prefix = f"{channel}_"
    results = payload.get("results") or []
    accepted = {*golden["expected_file"], *golden.get("also_accept", [])}
    evidence_found, evidence_total = evidence_recall(golden["expected_evidence"], results)
    sources = [result.get("source") or {} for result in results]
    latency = payload.get("latencyMs") or {}
    rank = _expected_rank(results, accepted)
    row.update(
        {
            prefix + "rank": rank,
            prefix + "bundle_rank": _bundle_rank(results, set(golden["expected_bundle"])),
            prefix + "hit_at_1": int(bool(rank and rank <= 1)),
            prefix + "hit_at_3": int(bool(rank and rank <= 3)),
            prefix + "hit_at_5": int(bool(rank and rank <= 5)),
            prefix + "hit_at_20": int(bool(rank and rank <= 20)),
            prefix + "hit_at_30": int(bool(rank and rank <= 30)),
            prefix + "reciprocal_rank": round(1.0 / rank, 6) if rank else 0.0,
            prefix + "evidence_found": evidence_found,
            prefix + "evidence_total": evidence_total,
            prefix + "evidence_recall": (
                round(evidence_found / evidence_total, 6) if evidence_total else None
            ),
            prefix + "wall_latency_ms": round(wall_latency_ms, 2),
            prefix + "server_total_ms": latency.get("total"),
            prefix + "server_retrieve_ms": latency.get("retrieve"),
            prefix + "server_rank_ms": latency.get("rank"),
            prefix + "candidate_count": payload.get("candidateCount"),
            prefix + "score_type": payload.get("scoreType"),
            prefix + "service_revision": payload.get("serviceRevision"),
            prefix + "server_commit": payload.get("commit"),
            prefix + "server_git_dirty": payload.get("gitDirty"),
            prefix + "ranked_file_ids": _json_cell([source.get("fileId") for source in sources]),
            prefix + "ranked_names": _json_cell([source.get("name") for source in sources]),
            prefix + "error": "",
        }
    )


def add_channel_error(row: dict[str, Any], channel: str, error: Exception, wall_ms: float) -> None:
    prefix = f"{channel}_"
    row.update(
        {
            prefix + "rank": None,
            prefix + "bundle_rank": None,
            prefix + "hit_at_1": 0,
            prefix + "hit_at_3": 0,
            prefix + "hit_at_5": 0,
            prefix + "hit_at_20": 0,
            prefix + "hit_at_30": 0,
            prefix + "reciprocal_rank": 0.0,
            prefix + "evidence_found": 0,
            prefix + "evidence_total": None,
            prefix + "evidence_recall": None,
            prefix + "wall_latency_ms": round(wall_ms, 2),
            prefix + "server_total_ms": None,
            prefix + "server_retrieve_ms": None,
            prefix + "server_rank_ms": None,
            prefix + "candidate_count": 0,
            prefix + "score_type": "",
            prefix + "service_revision": "",
            prefix + "server_commit": "",
            prefix + "server_git_dirty": None,
            prefix + "ranked_file_ids": "[]",
            prefix + "ranked_names": "[]",
            prefix + "error": f"{type(error).__name__}: {error}",
        }
    )


def summarize(rows: list[dict[str, Any]], channels: list[str]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for channel in channels:
        prefix = f"{channel}_"
        n = len(rows)
        latencies = [float(row[prefix + "wall_latency_ms"]) for row in rows]
        evidence_found = sum(int(row[prefix + "evidence_found"] or 0) for row in rows)
        evidence_total = sum(int(row[prefix + "evidence_total"] or 0) for row in rows)
        output[channel] = {
            "n": n,
            "errors": sum(bool(row[prefix + "error"]) for row in rows),
            "hit@1": sum(int(row[prefix + "hit_at_1"]) for row in rows),
            "hit@3": sum(int(row[prefix + "hit_at_3"]) for row in rows),
            "hit@5": sum(int(row[prefix + "hit_at_5"]) for row in rows),
            "hit@20": sum(int(row[prefix + "hit_at_20"]) for row in rows),
            "hit@30": sum(int(row[prefix + "hit_at_30"]) for row in rows),
            "mrr": round(
                statistics.fmean(float(row[prefix + "reciprocal_rank"]) for row in rows), 4
            ),
            "evidence_recall": round(evidence_found / evidence_total, 4)
            if evidence_total
            else None,
            "latency_mean_ms": round(statistics.fmean(latencies), 2),
            "latency_p95_ms": round(sorted(latencies)[round((n - 1) * 0.95)], 2),
        }
    return output


def write_flat_tables(out_dir: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "rows.csv"
    jsonl_path = out_dir / "rows.jsonl"
    fieldnames = list(rows[0]) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return csv_path, jsonl_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--split", choices=tuple(SPLIT_FILES), default="dev")
    parser.add_argument("--base-url", help="RAG MCP service root; /mcp suffix is accepted")
    parser.add_argument("--dept", help="read MCP_API_KEY from the Cloud department registry")
    parser.add_argument("--audience", choices=("staff", "student"), default="staff")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--candidate-k", type=int, default=30)
    parser.add_argument(
        "--channels",
        nargs="+",
        choices=RETRIEVAL_CHANNELS,
        default=list(RETRIEVAL_CHANNELS),
    )
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    if not 1 <= args.top_k <= 30 or not args.top_k <= args.candidate_k <= 30:
        parser.error("require 1 <= top-k <= candidate-k <= 30")
    if args.retries < 1:
        parser.error("retries must be at least 1")

    try:
        manifest, golden = load_snapshot(args.snapshot_dir, args.split)
        base_url = retrieval_base_url(args.base_url)
        commit, dirty = git_identity()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    key = (
        build_env(args.dept, args.audience)["MCP_API_KEY"]
        if args.dept
        else os.environ.get("MCP_API_KEY", "").strip()
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or ROOT / "tests" / "_bench_out" / "retrieval_channels" / run_id
    rows: list[dict[str, Any]] = []

    for position, golden_row in enumerate(golden, 1):
        row: dict[str, Any] = {
            "run_id": run_id,
            "snapshot_id": manifest["snapshot_id"],
            "snapshot_file": SPLIT_FILES[args.split],
            "snapshot_sha256": manifest["files"][SPLIT_FILES[args.split]],
            "split": args.split,
            "commit": commit,
            "git_dirty": dirty,
            "case_n": golden_row["n"],
            "query": golden_row["query"],
            "type": golden_row.get("type", ""),
            "weakness_tags": _json_cell(golden_row.get("weakness_tags", [])),
            "expected_file": _json_cell(golden_row["expected_file"]),
            "expected_bundle": _json_cell(golden_row["expected_bundle"]),
            "expected_evidence": _json_cell(golden_row["expected_evidence"]),
            "top_k": args.top_k,
            "candidate_k": args.candidate_k,
            "retrieval_base_url": base_url,
        }
        for channel in args.channels:
            started = time.perf_counter()
            error: Exception | None = None
            payload: dict[str, Any] | None = None
            for attempt in range(args.retries):
                try:
                    payload = call_channel(
                        base_url,
                        key,
                        channel,
                        golden_row["query"],
                        args.top_k,
                        args.candidate_k,
                    )
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    error = exc
                    if attempt + 1 < args.retries:
                        time.sleep(2 * (attempt + 1))
            wall_ms = (time.perf_counter() - started) * 1000
            if payload is not None:
                add_channel_columns(row, channel, payload, golden_row, wall_ms)
            else:
                add_channel_error(row, channel, error or RuntimeError("unknown error"), wall_ms)
        rows.append(row)
        print(f"{position}/{len(golden)} case={golden_row['n']}", file=sys.stderr)
        if args.sleep:
            time.sleep(args.sleep)

    csv_path, jsonl_path = write_flat_tables(out_dir, rows)
    summary = {
        "run_id": run_id,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_sha256": manifest["files"][SPLIT_FILES[args.split]],
        "split": args.split,
        "commit": commit,
        "git_dirty": dirty,
        "top_k": args.top_k,
        "candidate_k": args.candidate_k,
        "channels": summarize(rows, args.channels),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"flat CSV: {csv_path}\nflat JSONL: {jsonl_path}\nsummary: {summary_path}")
    return 1 if any(data["errors"] for data in summary["channels"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
