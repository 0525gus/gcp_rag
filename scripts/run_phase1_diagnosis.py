"""Run the frozen-corpus Phase-1 retrieval diagnosis without FactChat or an LLM.

The run order is fixed and persisted as separate tables:

  01 gold audit -> 02 k=50/100/200 curves -> 03 oracle union recall
  -> 04 Q1/Q2/Q3/Q4 diagnosis -> 05 leave-one-out attribution

The script reads the configured corpus, Firestore document state, and source GCS
objects directly.  It never writes to those systems and never persists source
body text in the result directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_retrieval_channels import DEFAULT_SNAPSHOT, SPLIT_FILES, load_snapshot
from shared.config import Settings
from shared.firestore_state import DocStateStore
from shared.gcs import GcsClient, gs_uri
from shared.lexical_rerank import Bm25Index, TitleBm25Index, normalize_title
from shared.metadata_rank import metadata_feature_scores
from shared.models import DocState
from shared.search_postprocess import extract_file_id
from shared.temporal_rank import temporal_scores

K_VALUES = (50, 100, 200)
VECTOR_MAX_K = 100  # Vertex RAG retrieveContexts API maximum; do not synthesize k=200.
LEXICAL_CHANNELS = ("body", "title", "bundle")
CHANNELS = ("vector", *LEXICAL_CHANNELS, "metadata")
PROMPT_HASH = "sha256:" + hashlib.sha256(b"phase1-deterministic-q1-q4-v1").hexdigest()
_WS = re.compile(r"\s+")
_MONEY = re.compile(r"(?<!\d)(\d[\d, ]*)\s*(만원|천원|원)(?![가-힣])")
_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})(?:일)?"
)


@dataclass(frozen=True)
class InventoryDocument:
    file_id: str
    title: str
    bundle: str
    path: str
    mime_type: str
    status: str
    indexed: bool
    rag_file_count: int
    source_object_count: int
    body: str

    @property
    def metadata(self) -> str:
        return " ".join(
            value
            for value in (
                normalize_title(self.title),
                normalize_title(self.bundle),
                normalize_title(self.path),
                self.mime_type,
            )
            if value
        )


@dataclass(frozen=True)
class RunContext:
    settings: Settings
    department: str
    audience: str
    drive_ids: frozenset[str]
    snapshot_id: str
    snapshot_sha256: str
    split: str
    code_commit: str
    code_dirty: bool
    started_utc: str
    credentials: Any
    access_token: str


@dataclass(frozen=True)
class ChannelIndexes:
    """Corpus-side lexical statistics built once for the frozen run."""

    body: Bm25Index
    title: TitleBm25Index
    bundle: TitleBm25Index


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _gcloud_access_credentials() -> tuple[Any, str]:
    """Use the active gcloud user token without requiring persisted local ADC."""
    from google.oauth2.credentials import Credentials

    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        raise RuntimeError("gcloud is required for the local Phase-1 run")
    check = subprocess.run(
        [gcloud, "auth", "print-access-token", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if check.returncode != 0 or not check.stdout.strip():
        detail = (check.stderr or check.stdout or "token unavailable").strip().splitlines()[-1]
        raise RuntimeError(
            "gcloud access token is unavailable: "
            f"{detail}. Run `gcloud auth login` before the local Phase-1 run."
        )
    token = check.stdout.strip()
    return Credentials(token), token


def _common_settings() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read the unified registry without printing its API keys."""
    from scripts.dept_gui import _common, _mcp_registry_read

    _record, configs = _mcp_registry_read()
    return _common(), configs


def _settings_from_registry(department: str, audience: str) -> tuple[Settings, frozenset[str]]:
    common, configs = _common_settings()
    config = configs.get(department)
    if not config:
        raise ValueError(f"department not found in unified registry: {department}")
    corpus = str((config.get("corpora") or {}).get(audience) or "").strip()
    source_bucket = str((config.get("buckets") or {}).get("source") or "").strip()
    drives = frozenset(str(value) for value in (config.get("drive") or {}).get("driveIds", []))
    if not corpus or not source_bucket or not drives:
        raise ValueError(
            f"{department}/{audience}: corpus, source bucket, and drive IDs are required"
        )
    return (
        Settings(
            gcp_project_id=str(common["GCP_PROJECT_ID"]),
            gcp_region=str(common.get("GCP_REGION") or "asia-northeast3"),
            firestore_database=str(common.get("FIRESTORE_DATABASE") or "rag-sync-state"),
            doc_state_collection=str(common.get("DOC_STATE_COLLECTION") or "doc_state"),
            rag_corpus_name=corpus,
            gcs_source_bucket=source_bucket,
            rag_chunk_size=int(common.get("RAG_CHUNK_SIZE") or 1024),
            rag_chunk_overlap=int(common.get("RAG_CHUNK_OVERLAP") or 256),
        ),
        drives,
    )


def normalized_evidence(value: str) -> str:
    """Normalize whitespace, dates, and Korean money units for audit matching."""
    text = html.unescape(value or "").casefold()

    def date_repl(match: re.Match[str]) -> str:
        return f" date{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d} "

    def money_repl(match: re.Match[str]) -> str:
        amount = int(re.sub(r"[, ]", "", match.group(1)))
        multiplier = {"만원": 10_000, "천원": 1_000, "원": 1}[match.group(2)]
        return f" moneywon{amount * multiplier} "

    text = _DATE.sub(date_repl, text)
    text = _MONEY.sub(money_repl, text)
    # Keep bare 4+ digit numbers so 3,000,000 and 3000000 compare after commas vanish.
    text = re.sub(r"(?<!\d)(\d[\d, ]{3,})(?!\d)", lambda m: re.sub(r"[, ]", "", m.group(1)), text)
    return _WS.sub(" ", text).strip()


def evidence_status(expected: list[Any], body: str) -> tuple[int, int]:
    corpus = normalized_evidence(body)
    found = 0
    for unit in expected:
        alternatives = unit if isinstance(unit, list) else [unit]
        if any(
            normalized_evidence(str(alternative)) in corpus
            for alternative in alternatives
            if normalized_evidence(str(alternative))
        ):
            found += 1
    return found, len(expected)


def audit_status(*, indexed: bool, source_count: int, body: str, found: int, total: int) -> str:
    if not indexed:
        return "not_indexed"
    if source_count == 0:
        # A RAG file exists, so this is not evidence that its index has zero
        # chunks. Vertex does not expose per-file chunk counts; keep the missing
        # source artifact auditable without incorrectly tripping the ingestion gate.
        return "source_object_missing"
    if not body.strip():
        return "empty_extraction"
    if total and found < total:
        return "evidence_extraction_failed"
    return "evidence_present"


def _states(store: DocStateStore, drive_ids: frozenset[str]) -> dict[str, DocState]:
    rows: dict[str, DocState] = {}
    # Audit deliberately uses a complete, read-only stream; Firestore `in` would
    # require chunking and can silently miss a newly added drive ID.
    for snapshot in store._col.stream():
        data = snapshot.to_dict() or {}
        data.setdefault("fileId", snapshot.id)
        state = DocState.from_firestore(data)
        if state.drive_id in drive_ids:
            rows[state.file_id] = state
    return rows


def _rag_file_counts(access_token: str, settings: Settings) -> dict[str, int]:
    """List RAG files with a hard timeout per page.

    The agentplatform pager can wait indefinitely while retrying a stalled list
    request.  This audit is read-only and must fail visibly rather than make a
    local evaluation appear frozen, so it calls the documented REST list route
    directly and preserves the page token itself.
    """
    counts: dict[str, int] = {}
    page_token = ""
    while True:
        query = urllib.parse.urlencode(
            {key: value for key, value in {"pageSize": 100, "pageToken": page_token}.items() if value}
        )
        url = (
            f"https://{settings.gcp_region}-aiplatform.googleapis.com/v1/"
            f"{settings.rag_corpus_name}/ragFiles?{query}"
        )
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access_token}"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"RAG file list HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("RAG file list request failed") from exc
        for item in payload.get("ragFiles") or []:
            if not isinstance(item, dict):
                continue
            display = str(item.get("displayName") or "")
            sources = item.get("gcsSource") or {}
            uris = sources.get("uris") if isinstance(sources, dict) else []
            source_uri = str((uris or [""])[0])
            file_id = extract_file_id(display, source_uri)
            if file_id:
                counts[file_id] = counts.get(file_id, 0) + 1
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            break
    return counts


def _source_names(gcs: GcsClient, indexed_ids: set[str]) -> dict[str, list[str]]:
    from shared.search_postprocess import extract_file_id

    names: dict[str, list[str]] = {file_id: [] for file_id in indexed_ids}
    bucket = gcs._client.bucket(gcs.settings.gcs_source_bucket)
    for blob in gcs._client.list_blobs(bucket):
        name = str(blob.name)
        if not name.lower().endswith(".md") or name.lower().endswith(".meta.md"):
            continue
        file_id = extract_file_id(name, gs_uri(gcs.settings.gcs_source_bucket, name))
        if file_id in names:
            names[file_id].append(name)
    return names


def _download_source(gcs: GcsClient, names: list[str]) -> str:
    parts: list[str] = []
    for name in sorted(names):
        try:
            parts.append(
                gcs.download_bytes(gs_uri(gcs.settings.gcs_source_bucket, name)).decode("utf-8")
            )
        except UnicodeDecodeError:
            parts.append(
                gcs.download_bytes(gs_uri(gcs.settings.gcs_source_bucket, name)).decode(
                    "utf-8", "replace"
                )
            )
    return "\n\n".join(parts).strip()


def build_inventory(
    settings: Settings,
    drive_ids: frozenset[str],
    workers: int,
    credentials: Any,
    access_token: str,
) -> tuple[dict[str, InventoryDocument], str]:
    print("[1/5] inventory: reading Firestore document state", flush=True)
    store = DocStateStore(settings, credentials=credentials)
    states = _states(store, drive_ids)
    print("[1/5] inventory: listing Vertex RAG files", flush=True)
    rag_counts = _rag_file_counts(access_token, settings)
    indexed_ids = set(rag_counts)
    print("[1/5] inventory: listing source Markdown objects", flush=True)
    gcs = GcsClient(settings, credentials=credentials)
    source_names = _source_names(gcs, indexed_ids)
    bodies: dict[str, str] = {}
    source_files = {file_id: names for file_id, names in source_names.items() if names}
    print(
        f"[1/5] inventory: downloading {len(source_files)} source documents with {workers} workers",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_download_source, gcs, names): file_id
            for file_id, names in source_files.items()
        }
        for completed, future in enumerate(as_completed(futures), 1):
            file_id = futures[future]
            try:
                bodies[file_id] = future.result()
            except Exception:  # noqa: BLE001 - one unreadable source must not abort corpus audit
                bodies[file_id] = ""
            if completed % 50 == 0 or completed == len(futures):
                print(f"[1/5] inventory: source {completed}/{len(futures)}", flush=True)
    docs: dict[str, InventoryDocument] = {}
    for file_id in indexed_ids | set(states):
        state = states.get(file_id)
        docs[file_id] = InventoryDocument(
            file_id=file_id,
            title=(state.name if state else "") or file_id,
            bundle=(state.bundle if state else "") or "",
            path=(state.path if state else "") or "",
            mime_type=(state.mime_type if state else "") or "",
            status=(state.status.value if state else "MISSING_DOC_STATE"),
            indexed=file_id in indexed_ids,
            rag_file_count=rag_counts.get(file_id, 0),
            source_object_count=len(source_names.get(file_id, [])),
            body=bodies.get(file_id, ""),
        )
    signature = _sha256_text(
        "\n".join(
            f"{doc.file_id}|{doc.rag_file_count}|{doc.status}|{len(doc.body)}"
            for doc in sorted(docs.values(), key=lambda value: value.file_id)
        )
    )
    return docs, signature


def _ranked_nonzero(scores: list[float], docs: list[InventoryDocument], k: int) -> list[str]:
    order = sorted(range(len(docs)), key=lambda index: (-scores[index], docs[index].file_id))
    return [docs[index].file_id for index in order[:k] if scores[index] > 0]


def build_channel_indexes(docs: list[InventoryDocument]) -> ChannelIndexes:
    """Build fixed-corpus lexical indexes once, before evaluating all queries."""
    return ChannelIndexes(
        body=Bm25Index([doc.body for doc in docs]),
        title=TitleBm25Index([doc.title for doc in docs]),
        bundle=TitleBm25Index([doc.bundle for doc in docs]),
    )


def lexical_candidates(
    query: str,
    docs: list[InventoryDocument],
    channel: str,
    k: int,
    *,
    indexes: ChannelIndexes | None = None,
) -> list[str]:
    if channel == "body":
        scores = (indexes.body if indexes else Bm25Index([doc.body for doc in docs])).scores(query)
    elif channel == "title":
        scores = (indexes.title if indexes else TitleBm25Index([doc.title for doc in docs])).scores(query)
    elif channel == "bundle":
        scores = (indexes.bundle if indexes else TitleBm25Index([doc.bundle for doc in docs])).scores(query)
    elif channel == "metadata":
        periods = temporal_scores(query, [doc.metadata for doc in docs])
        features = metadata_feature_scores(query, [doc.metadata for doc in docs])
        scores = [
            period + feature.sequence + feature.document_kind + feature.revision
            for period, feature in zip(periods, features, strict=True)
        ]
    else:
        raise ValueError(f"unknown channel: {channel}")
    return _ranked_nonzero(scores, docs, k)


def vector_candidates(
    access_token: str, settings: Settings, query: str, k: int
) -> tuple[list[str], str, float]:
    if k > VECTOR_MAX_K:
        return [], "unsupported_by_vertex_top_k_100", 0.0
    started = time.perf_counter()
    url = (
        f"https://{settings.gcp_region}-aiplatform.googleapis.com/v1/projects/"
        f"{settings.gcp_project_id}/locations/{settings.gcp_region}:retrieveContexts"
    )
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {
                    "vertexRagStore": {"ragResources": [{"ragCorpus": settings.rag_corpus_name}]},
                    "query": {"text": query, "ragRetrievalConfig": {"topK": k}},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"http_{exc.code}", (time.perf_counter() - started) * 1000
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return [], "request_failed", (time.perf_counter() - started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    contexts = (payload.get("contexts") or {}).get("contexts") or []
    result: list[str] = []
    seen: set[str] = set()
    for context in contexts:
        if not isinstance(context, dict):
            continue
        source_uri = str(context.get("sourceUri") or context.get("source_uri") or "")
        display = str(context.get("sourceDisplayName") or context.get("source_display_name") or "")
        file_id = extract_file_id(display, source_uri)
        if file_id and file_id not in seen:
            result.append(file_id)
            seen.add(file_id)
    return result, "ok", elapsed_ms


def _rank(candidates: Iterable[str], accepted: set[str]) -> int | None:
    return next((index for index, file_id in enumerate(candidates, 1) if file_id in accepted), None)


def _common_fields(context: RunContext, index_version: str) -> dict[str, Any]:
    return {
        "snapshot_id": context.snapshot_id,
        "snapshot_sha256": context.snapshot_sha256,
        "split": context.split,
        "index_version": index_version,
        "code_commit": context.code_commit,
        "code_dirty": context.code_dirty,
        "model_version": "vertex-rag-engine",
        "prompt_hash": PROMPT_HASH,
        "department": context.department,
        "audience": context.audience,
        "started_utc": context.started_utc,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _add_rank_columns(
    row: dict[str, Any], prefix: str, candidates: dict[str, list[str]], accepted: set[str]
) -> None:
    for channel, values in candidates.items():
        rank = _rank(values, accepted)
        row[f"{prefix}_{channel}_rank"] = rank
        row[f"{prefix}_{channel}_hit"] = bool(rank)
    union = set().union(*map(set, candidates.values())) if candidates else set()
    row[f"{prefix}_union_hit"] = bool(union & accepted)


def _diagnosis_label(q1: bool, q2: bool, q3: bool, q4: bool) -> str:
    if not q4:
        return "ingestion_or_index_pipeline"
    if not q1 and q2:
        return "query_side_normalization_or_expansion"
    if not q2 and q4:
        return "lexical_analyzer_or_field_mapping"
    if not q1 and q3:
        return "filename_normalization_or_field_mapping"
    if not q1:
        return "candidate_generation_or_ranking"
    return "retrievable_from_user_query"


def run(context: RunContext, *, source_workers: int, out_dir: Path) -> int:
    manifest, golden = load_snapshot(DEFAULT_SNAPSHOT, context.split)
    if manifest["snapshot_id"] != context.snapshot_id:
        raise RuntimeError("run context does not match frozen Golden snapshot")
    print("[1/5] gold audit inventory: Firestore + Vertex file list + source GCS", flush=True)
    inventory, index_version = build_inventory(
        context.settings,
        context.drive_ids,
        source_workers,
        context.credentials,
        context.access_token,
    )
    docs = sorted((doc for doc in inventory.values() if doc.indexed), key=lambda doc: doc.file_id)
    common = _common_fields(context, index_version)

    # 01. Gold retrievability audit.
    print("[1/5] gold retrievability audit", flush=True)
    audit_rows: list[dict[str, Any]] = []
    for position, item in enumerate(golden, 1):
        accepted = {*item["expected_file"], *item.get("also_accept", [])}
        candidates = [inventory[file_id] for file_id in accepted if file_id in inventory]
        indexed = any(candidate.indexed for candidate in candidates)
        source_count = sum(candidate.source_object_count for candidate in candidates)
        body = "\n".join(candidate.body for candidate in candidates)
        found, total = evidence_status(item["expected_evidence"], body)
        audit_rows.append(
            {
                **common,
                "query_id": item["n"],
                "query": item["query"],
                "weakness_tags": json.dumps(item.get("weakness_tags", []), ensure_ascii=False),
                "annotation_status": item.get("annotation_status", "valid"),
                "gold_file_id": json.dumps(sorted(accepted)),
                "indexed": indexed,
                "rag_file_count": sum(candidate.rag_file_count for candidate in candidates),
                "chunk_count": "",
                "chunk_count_basis": "not_exposed_by_vertex_rag",
                "source_object_count": source_count,
                "extracted_chars": len(body),
                "evidence_found": found,
                "evidence_total": total,
                "evidence_status": audit_status(
                    indexed=indexed,
                    source_count=source_count,
                    body=body,
                    found=found,
                    total=total,
                ),
            }
        )
    _write_csv(out_dir / "01_gold_audit_rows.csv", audit_rows)
    audit_summary = [
        {
            **common,
            "status": status,
            "queries": sum(row["evidence_status"] == status for row in audit_rows),
        }
        for status in (
            "not_indexed",
            "no_chunks",
            "source_object_missing",
            "empty_extraction",
            "evidence_extraction_failed",
            "evidence_present",
        )
    ]
    _write_csv(out_dir / "01_gold_audit_summary.csv", audit_summary)
    blocking = sum(
        row["evidence_status"]
        in {"not_indexed", "no_chunks", "empty_extraction", "evidence_extraction_failed"}
        for row in audit_rows
    )
    if blocking / len(audit_rows) > 0.10:
        (out_dir / "STOPPED.txt").write_text(
            f"ingestion gate: {blocking}/{len(audit_rows)} > 10%; fix ingestion before ceiling analysis\n",
            encoding="utf-8",
        )
        return 3

    print("[1/5] building reusable BM25 indexes", flush=True)
    indexes = build_channel_indexes(docs)

    # 02. Large-fetch curves.  q1_cache is reused by later union and diagnosis.
    print("[2/5] large-fetch curves", flush=True)
    curves: list[dict[str, Any]] = []
    q1_cache: dict[int, dict[str, list[str]]] = {}
    for position, item in enumerate(golden, 1):
        query_id = int(item["n"])
        accepted = {*item["expected_file"], *item.get("also_accept", [])}
        vectors, vector_status, vector_latency = vector_candidates(
            context.access_token, context.settings, item["query"], VECTOR_MAX_K
        )
        per_channel = {"vector": vectors}
        per_latency = {"vector": vector_latency}
        for channel in (*LEXICAL_CHANNELS, "metadata"):
            started = time.perf_counter()
            per_channel[channel] = lexical_candidates(
                item["query"],
                docs,
                channel,
                len(docs) if channel == "metadata" else 200,
                indexes=indexes,
            )
            per_latency[channel] = (time.perf_counter() - started) * 1000
        q1_cache[query_id] = per_channel
        if position % 10 == 0 or position == len(golden):
            print(f"[2/5] curve {position}/{len(golden)}", flush=True)
        for channel in CHANNELS:
            for k in K_VALUES:
                if channel == "metadata":
                    curves.append(
                        {
                            **common,
                            "query_id": query_id,
                            "channel": channel,
                            "k": "exact",
                            "status": "ok",
                            "candidate_count": len(per_channel[channel]),
                            "gold_included": bool(set(per_channel[channel]) & accepted),
                            "latency_ms": round(per_latency[channel], 2),
                        }
                    )
                    break
                if channel == "vector" and k > VECTOR_MAX_K:
                    curves.append(
                        {
                            **common,
                            "query_id": query_id,
                            "channel": channel,
                            "k": k,
                            "status": "unsupported_by_vertex_top_k_100",
                            "candidate_count": "",
                            "gold_included": "",
                            "latency_ms": "",
                        }
                    )
                    continue
                values = per_channel[channel][:k]
                curves.append(
                    {
                        **common,
                        "query_id": query_id,
                        "channel": channel,
                        "k": k,
                        "status": vector_status if channel == "vector" else "ok",
                        "candidate_count": len(values),
                        "gold_included": bool(set(values) & accepted),
                        "latency_ms": round(per_latency[channel], 2),
                    }
                )
    _write_csv(out_dir / "02_large_fetch_curve_rows.csv", curves)
    curve_summary = []
    for channel in CHANNELS:
        for k in ("exact", *K_VALUES):
            subset = [row for row in curves if row["channel"] == channel and row["k"] == k]
            if not subset:
                continue
            ok = [row for row in subset if row["status"] == "ok"]
            curve_summary.append(
                {
                    **common,
                    "channel": channel,
                    "k": k,
                    "status": ok[0]["status"] if ok else subset[0]["status"],
                    "queries": len(ok),
                    "recall": round(sum(bool(row["gold_included"]) for row in ok) / len(ok), 4)
                    if ok
                    else "",
                    "latency_mean_ms": round(
                        sum(float(row["latency_ms"]) for row in ok) / len(ok), 2
                    )
                    if ok
                    else "",
                }
            )
    _write_csv(out_dir / "02_large_fetch_curve_summary.csv", curve_summary)

    # 03. Oracle union recall, before ranking/pruning.
    print("[3/5] oracle union recall", flush=True)
    union_rows: list[dict[str, Any]] = []
    for position, item in enumerate(golden, 1):
        query_id = int(item["n"])
        accepted = {*item["expected_file"], *item.get("also_accept", [])}
        channels = q1_cache[query_id]
        all_sets = {channel: set(values) for channel, values in channels.items()}
        union = set().union(*all_sets.values())
        union_rows.append(
            {
                **common,
                "query_id": query_id,
                "weakness_tags": json.dumps(item.get("weakness_tags", []), ensure_ascii=False),
                "gold_file_id": json.dumps(sorted(accepted)),
                "union_hit": bool(union & accepted),
                "union_candidate_count": len(union),
                **{
                    f"{channel}_hit": bool(values & accepted)
                    for channel, values in all_sets.items()
                },
            }
        )
    _write_csv(out_dir / "03_oracle_union_rows.csv", union_rows)
    _write_csv(
        out_dir / "03_oracle_union_summary.csv",
        [
            {
                **common,
                "queries": len(union_rows),
                "oracle_union_recall": round(
                    sum(row["union_hit"] for row in union_rows) / len(union_rows), 4
                ),
            }
        ],
    )

    # 04. Q1/Q2/Q3/Q4 query diagnosis.  Vector Q1 is reused; Q2/Q3 are actual new calls.
    print("[4/5] Q1/Q2/Q3/Q4 diagnosis", flush=True)
    diagnosis_rows: list[dict[str, Any]] = []
    for position, item in enumerate(golden, 1):
        query_id = int(item["n"])
        accepted = {*item["expected_file"], *item.get("also_accept", [])}
        gold = next((inventory[file_id] for file_id in accepted if file_id in inventory), None)
        q1 = item["query"]
        q2 = gold.title if gold else ""
        q3 = normalize_title(q2)
        variants = {"q1": q1, "q2": q2, "q3": q3}
        candidate_maps: dict[str, dict[str, list[str]]] = {"q1": q1_cache[query_id]}
        for label in ("q2", "q3"):
            if not variants[label].strip():
                candidate_maps[label] = {channel: [] for channel in CHANNELS}
                continue
            vector, _status, _latency = vector_candidates(
                context.access_token, context.settings, variants[label], VECTOR_MAX_K
            )
            candidate_maps[label] = {"vector": vector}
            for channel in (*LEXICAL_CHANNELS, "metadata"):
                candidate_maps[label][channel] = lexical_candidates(
                    variants[label],
                    docs,
                    channel,
                    len(docs) if channel == "metadata" else 200,
                    indexes=indexes,
                )
        row: dict[str, Any] = {
            **common,
            "query_id": query_id,
            "query": q1,
            "q2_exact_gold_title": q2,
            "q3_normalized_gold_filename": q3,
            "q4_direct_file_lookup": bool(gold and gold.indexed),
            "weakness_tags": json.dumps(item.get("weakness_tags", []), ensure_ascii=False),
        }
        for label, candidates in candidate_maps.items():
            _add_rank_columns(row, label, candidates, accepted)
        row["diagnosis"] = _diagnosis_label(
            bool(row["q1_union_hit"]),
            bool(row["q2_union_hit"]),
            bool(row["q3_union_hit"]),
            bool(row["q4_direct_file_lookup"]),
        )
        diagnosis_rows.append(row)
        if position % 10 == 0 or position == len(golden):
            print(f"[4/5] diagnosis {position}/{len(golden)}", flush=True)
    _write_csv(out_dir / "04_q1_q4_diagnosis_rows.csv", diagnosis_rows)
    diagnosis_summary = [
        {
            **common,
            "diagnosis": label,
            "queries": sum(row["diagnosis"] == label for row in diagnosis_rows),
        }
        for label in sorted({row["diagnosis"] for row in diagnosis_rows})
    ]
    _write_csv(out_dir / "04_q1_q4_diagnosis_summary.csv", diagnosis_summary)

    # 05. Leave-one-out attribution over the same max-K candidate union.
    print("[5/5] leave-one-out attribution", flush=True)
    attribution_rows: list[dict[str, Any]] = []
    for channel in CHANNELS:
        full_hits = 0
        loo_hits = 0
        unique_rescue = 0
        rescue_tags: dict[str, int] = {}
        for item in golden:
            accepted = {*item["expected_file"], *item.get("also_accept", [])}
            sets = {name: set(values) for name, values in q1_cache[int(item["n"])].items()}
            full = set().union(*sets.values())
            without = set().union(*(values for name, values in sets.items() if name != channel))
            full_hits += bool(full & accepted)
            loo_hits += bool(without & accepted)
            if sets[channel] & accepted and not (without & accepted):
                unique_rescue += 1
                for tag in item.get("weakness_tags", []):
                    rescue_tags[tag] = rescue_tags.get(tag, 0) + 1
        attribution_rows.append(
            {
                **common,
                "channel": channel,
                "full_union_recall": round(full_hits / len(golden), 4),
                "loo_recall": round(loo_hits / len(golden), 4),
                "loo_delta_recall": round((full_hits - loo_hits) / len(golden), 4),
                "unique_rescue": unique_rescue,
                "rescue_weakness_tags": json.dumps(rescue_tags, ensure_ascii=False, sort_keys=True),
                "decision_hint": "keep"
                if unique_rescue >= 5
                else "review"
                if unique_rescue >= 2
                else "remove_candidate",
            }
        )
    _write_csv(out_dir / "05_loo_channel_attribution.csv", attribution_rows)

    # Confirm the index did not change during the multi-hour diagnostic run.
    ending_counts = _rag_file_counts(
        context.access_token, context.settings
    )
    ending_version = _sha256_text(
        "\n".join(f"{key}|{value}" for key, value in sorted(ending_counts.items()))
    )
    started_rag_version = _sha256_text(
        "\n".join(
            f"{key}|{value}"
            for key, value in sorted(
                (file_id, document.rag_file_count)
                for file_id, document in inventory.items()
                if document.indexed
            )
        )
    )
    metadata = {
        **common,
        "corpus_snapshot_id": f"rag:{context.settings.rag_corpus_name.rsplit('/', 1)[-1]}:{index_version.split(':', 1)[1][:12]}",
        "inventory_documents": len(docs),
        "audit_blocking_failures": blocking,
        "oracle_union_recall": round(
            sum(row["union_hit"] for row in union_rows) / len(union_rows), 4
        ),
        "vector_max_k": VECTOR_MAX_K,
        "vector_k200": "unsupported_by_vertex_top_k_100",
        "rag_files_unchanged": started_rag_version == ending_version,
        "ended_utc": datetime.now(UTC).isoformat(),
    }
    (out_dir / "00_run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if metadata["rag_files_unchanged"] else 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--department", required=True, help="unified registry department code")
    parser.add_argument("--audience", default="staff", choices=("staff", "student"))
    parser.add_argument("--split", default="dev", choices=tuple(SPLIT_FILES))
    parser.add_argument("--source-workers", type=int, default=8)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    if args.source_workers < 1 or args.source_workers > 32:
        parser.error("source-workers must be between 1 and 32")
    manifest, _golden = load_snapshot(DEFAULT_SNAPSHOT, args.split)
    try:
        credentials, access_token = _gcloud_access_credentials()
        settings, drives = _settings_from_registry(args.department, args.audience)
        commit, dirty = _git_identity()
    except Exception as exc:  # noqa: BLE001 - registry/auth failures need one CLI error path
        print(f"cannot initialize Phase-1 run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or ROOT / "tests" / "_bench_out" / "phase1" / run_id
    context = RunContext(
        settings=settings,
        department=args.department,
        audience=args.audience,
        drive_ids=drives,
        snapshot_id=manifest["snapshot_id"],
        snapshot_sha256=manifest["files"][SPLIT_FILES[args.split]],
        split=args.split,
        code_commit=commit,
        code_dirty=dirty,
        started_utc=datetime.now(UTC).isoformat(),
        credentials=credentials,
        access_token=access_token,
    )
    try:
        code = run(context, source_workers=args.source_workers, out_dir=out_dir)
    except Exception as exc:  # noqa: BLE001 - preserve tables only on a complete diagnostic run
        print(f"Phase-1 failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Phase-1 tables: {out_dir}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
