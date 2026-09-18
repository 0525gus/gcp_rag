"""One-shot deployed-MCP context capture and evidence-recall diagnosis for dev150.

This script deliberately reads only the frozen dev split and invokes the normal
MCP ``search`` tool once per query.  It never calls FactChat, never reads the
holdout split, and does not serialize credentials.  The JSONL context artifact
contains the complete decoded structured context returned for each request.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_golden import normalize_golden
from shared.search_response import response_documents

SNAPSHOT = ROOT / "tests" / "frozen" / "golden200-2026-09-13"
DEV = SNAPSHOT / "golden200_dev150.json"
BASELINE = ROOT / "tests" / "_bench_out" / "operating_baseline" / "dev150-full-20260913T172415Z"
PHASE1 = ROOT / "tests" / "_bench_out" / "phase1" / "20260913T025120Z" / "01_gold_audit_rows.csv"
OUT = ROOT / "tests" / "_bench_out" / "evidence_diagnosis" / "dev150-20260913"
_WS = re.compile(r"\s+")
_MONEY = re.compile(r"(?<!\d)(\d[\d, ]*)\s*(만원|천원|원)(?![가-힣])")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})(?:일)?")
_SEMESTER = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(?:학년도|년도|년)?\s*(?:제\s*)?([12])\s*학기")
_BARE_SEMESTER = re.compile(r"(?<![\d가-힣])(?:제\s*)?([12])\s*학기")


def legacy_normalize(value: str) -> str:
    """The baseline scorer's case/whitespace-only behavior."""
    return _WS.sub(" ", html.unescape(value or "").casefold()).strip()


def normalized_evidence(value: str) -> str:
    """Comparable evidence form including repo Phase-1 date/money conventions."""
    text = unicodedata.normalize("NFC", html.unescape(value or "")).casefold()

    def date(match: re.Match[str]) -> str:
        return f" date{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d} "

    def money(match: re.Match[str]) -> str:
        amount = int(re.sub(r"[, ]", "", match.group(1)))
        return f" moneywon{amount * {'만원': 10000, '천원': 1000, '원': 1}[match.group(2)]} "

    def semester(match: re.Match[str]) -> str:
        return f" semester{int(match.group(1)):04d}{match.group(2)} "

    text = _DATE.sub(date, text)
    text = _MONEY.sub(money, text)
    text = _SEMESTER.sub(semester, text)
    text = _BARE_SEMESTER.sub(lambda m: f" semester{m.group(1)} ", text)
    text = re.sub(r"(?<![\da-z])(\d(?:[\d, ]*\d){3,})(?![\da-z])", lambda m: re.sub(r"[, ]", "", m.group(1)), text)
    # Punctuation differences (including Korean/ASCII punctuation) are not evidence differences.
    text = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text)
    return _WS.sub(" ", text).strip()


def layout_normalized_evidence(value: str) -> str:
    """Corrected v2 matcher: layout symbols are separators, never evidence text.

    NFKC is intentional here: returned table/Markdown glyph variants should not
    make a long textual evidence anchor fail.  Alphanumeric content is retained;
    every other run becomes one separator before whitespace collapse.
    """
    text = unicodedata.normalize("NFKC", html.unescape(value or "")).casefold()

    def date(match: re.Match[str]) -> str:
        return f" date{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d} "

    def money(match: re.Match[str]) -> str:
        amount = int(re.sub(r"[, ]", "", match.group(1)))
        return f" moneywon{amount * {'만원': 10000, '천원': 1000, '원': 1}[match.group(2)]} "

    def semester(match: re.Match[str]) -> str:
        return f" semester{int(match.group(1)):04d}{match.group(2)} "

    text = _DATE.sub(date, text)
    text = _MONEY.sub(money, text)
    text = _SEMESTER.sub(semester, text)
    text = _BARE_SEMESTER.sub(lambda m: f" semester{m.group(1)} ", text)
    text = re.sub(r"(?<![\da-z])(\d(?:[\d, ]*\d){3,})(?![\da-z])", lambda m: re.sub(r"[, ]", "", m.group(1)), text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    # Python \w retains underscore; layout separators should include it as well.
    return _WS.sub(" ", text.replace("_", " ")).strip()


def _documents_from_raw(raw: str) -> list[dict[str, Any]]:
    data_lines = [line[5:].lstrip() for line in raw.splitlines() if line.lstrip().startswith("data:")]
    payload = json.loads("".join(data_lines) if data_lines else raw)
    documents = response_documents(payload.get("result", {}))
    for document in documents:
        for chunk in document.get("chunks", []):
            text = chunk.get("text", "")
            if isinstance(text, str) and text.startswith("zlib64:"):
                chunk["text"] = zlib.decompress(base64.b64decode(text[7:])).decode("utf-8")
    return documents


def call_search_once(url: str, key: str, query: str) -> tuple[list[dict[str, Any]], str]:
    request_body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "top_k": 20}},
    }
    request = urllib.request.Request(
        url, data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
    return _documents_from_raw(raw), "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evidence_matches(expected: list[Any], documents: list[dict[str, Any]], normalizer: Any) -> list[bool]:
    corpus = normalizer("\n".join(str(c.get("text", "")) for d in documents for c in d.get("chunks", [])))
    result = []
    for unit in expected:
        choices = unit if isinstance(unit, list) else [unit]
        result.append(any(normalizer(str(choice)) in corpus for choice in choices if normalizer(str(choice))))
    return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def _write_offline_metrics(
    golden: list[dict[str, Any]], excluded: dict[int, str], out: Path, context_path: Path
) -> None:
    """Re-score a prior one-shot context capture without making network calls."""
    contexts = {
        int(json.loads(line)["query_id"]): json.loads(line)
        for line in context_path.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    if set(contexts) != {int(item["n"]) for item in golden}:
        raise RuntimeError("offline context capture does not contain exactly the frozen dev150 query IDs")
    fact_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for item in golden:
        capture = contexts[int(item["n"])]
        documents = capture["documents"]
        legacy = evidence_matches(item["expected_evidence"], documents, legacy_normalize)
        v1 = evidence_matches(item["expected_evidence"], documents, normalized_evidence)
        layout = evidence_matches(item["expected_evidence"], documents, layout_normalized_evidence)
        status = excluded.get(int(item["n"]), "verifiable")
        for evidence_index, expected in enumerate(item["expected_evidence"], 1):
            fact_rows.append({"query_id": item["n"], "evidence_index": evidence_index, "query": item["query"], "verification_scope": status, "expected_alternatives": json.dumps(expected if isinstance(expected, list) else [expected], ensure_ascii=False), "legacy_found": legacy[evidence_index-1], "normalized_v1_found": v1[evidence_index-1], "layout_normalized_found": layout[evidence_index-1], "normalized_found": layout[evidence_index-1], "normalization_changed_match": legacy[evidence_index-1] != layout[evidence_index-1]})
        total = len(layout)
        found = {"legacy": sum(legacy), "normalized_v1": sum(v1), "layout_normalized": sum(layout)}
        row: dict[str, Any] = {"query_id": item["n"], "query": item["query"], "verification_scope": status, "evidence_total": total, "returned_document_count": capture["total_returned_documents"], "returned_chunk_count": capture["total_returned_chunks"], "response_sha256": capture["response_sha256"]}
        for method, count in found.items():
            row[f"{method}_evidence_found"] = count
            row[f"{method}_all_expected_present"] = count == total
            row[f"{method}_query_outcome"] = "all" if count == total else ("partial" if count else "none")
        # Backward-compatible primary aliases now point at the corrected matcher.
        row["normalized_evidence_found"] = found["layout_normalized"]
        row["normalized_all_expected_present"] = row["layout_normalized_all_expected_present"]
        row["normalized_query_outcome"] = row["layout_normalized_query_outcome"]
        query_rows.append(row)
    _write_csv(out / "01_evidence_fact_rows.csv", fact_rows)
    _write_csv(out / "01_evidence_query_rows.csv", query_rows)
    summary: list[dict[str, Any]] = []
    scopes = [("all_dev150", query_rows), ("verifiable", [r for r in query_rows if r["verification_scope"] == "verifiable"]), ("unverifiable_evidence_extraction_failed", [r for r in query_rows if r["verification_scope"] == "evidence_extraction_failed"]), ("unverifiable_source_object_missing", [r for r in query_rows if r["verification_scope"] == "source_object_missing"]), ("unverifiable_all", [r for r in query_rows if r["verification_scope"] != "verifiable"])]
    for scope, rows in scopes:
        for method in ("legacy", "normalized_v1", "layout_normalized"):
            found = sum(int(r[f"{method}_evidence_found"]) for r in rows); total = sum(int(r["evidence_total"]) for r in rows)
            summary.append({"scope": scope, "matching": method, "queries": len(rows), "fact_found": found, "fact_total": total, "evidence_recall": round(found / total, 6) if total else "", "all_expected_facts_present_queries": sum(r[f"{method}_query_outcome"] == "all" for r in rows), "partial_queries": sum(r[f"{method}_query_outcome"] == "partial" for r in rows), "none_queries": sum(r[f"{method}_query_outcome"] == "none" for r in rows)})
    _write_csv(out / "01_evidence_summary.csv", summary)
    (out / "01_notes.md").write_text("""# Dev150 evidence-recall context capture

- Frozen input: `tests/frozen/golden200-2026-09-13/golden200_dev150.json` (150 cases only); supplied baseline order was verified before the one-shot capture.
- Phase-1 exclusions remain explicit: 7 `evidence_extraction_failed` and 9 `source_object_missing`. They are included in `all_dev150`, reported separately, and excluded only in the explicit `verifiable` population.
- This revision rescored the existing `01_context_capture.jsonl` offline; it made **zero** MCP, FactChat, or holdout requests.
- Primary result is `layout_normalized`: NFKC; date/money/semester canonicalization; then every non-alphanumeric layout/symbol run (Markdown pipes, angle brackets, bullets, dashes, CSV/table delimiters) becomes one separator before whitespace collapse. `normalized_v1` preserves the prior result for audit comparison. Long substring anchors are still required; this is not semantic entailment.
- Fact rows preserve one row per annotation and fact denominator. Query 117 has two semantically duplicate annotations; this is flagged as an annotation issue, but neither annotation was deduplicated or removed.
- `01_context_capture.jsonl` is unchanged from the one-shot deployed-MCP capture and contains source/rank/chunk diagnostics without credentials.
""", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--offline-from-context", type=Path, help="re-score an existing JSONL without retrieval")
    args = parser.parse_args()
    from scripts.measure_operating_baseline import _registry_scope

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    golden = [normalize_golden(row, i) for i, row in enumerate(json.loads(DEV.read_text(encoding="utf-8")), 1)]
    baseline = _read_csv(BASELINE / "baseline_rows.csv")
    if len(golden) != 150 or len(baseline) != 150 or [str(x["n"]) for x in golden] != [r["case_n"] for r in baseline]:
        raise RuntimeError("frozen dev150 and supplied baseline do not have the same 150 ordered cases")
    audit = {int(row["query_id"]): row["evidence_status"] for row in _read_csv(PHASE1)}
    excluded = {qid: status for qid, status in audit.items() if status in {"evidence_extraction_failed", "source_object_missing"}}
    if len(excluded) != 16 or list(excluded.values()).count("evidence_extraction_failed") != 7 or list(excluded.values()).count("source_object_missing") != 9:
        raise RuntimeError(f"Phase-1 exclusion invariant failed: {excluded}")
    if not set(excluded).issubset({int(row["n"]) for row in golden}):
        raise RuntimeError("Phase-1 exclusions are not a subset of dev150")
    if args.offline_from_context:
        _write_offline_metrics(golden, excluded, out, args.offline_from_context)
        return 0

    url, key = _registry_scope("cs", "staff")
    captured_at = datetime.now(UTC).isoformat()
    fact_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    with (out / "01_context_capture.jsonl").open("w", encoding="utf-8") as context_file:
        for position, item in enumerate(golden, 1):
            documents, response_sha = call_search_once(url, key, item["query"])
            # Full decoded search context, plus compact hashes for downstream joins.
            context_file.write(json.dumps({
                "query_id": item["n"], "query": item["query"], "capture_utc": captured_at,
                "request": {"tool": "search", "top_k": 20}, "response_sha256": response_sha,
                "total_returned_documents": len(documents), "total_returned_chunks": sum(len(d.get("chunks", [])) for d in documents),
                "documents": [{"rank": rank, "source": document.get("source", {}), "chunk_count": len(document.get("chunks", [])), "chunks": [
                    {**chunk, "rank_within_document": ci, "text_sha256": "sha256:" + hashlib.sha256(str(chunk.get("text", "")).encode("utf-8")).hexdigest(), "normalized_text_sha256": "sha256:" + hashlib.sha256(normalized_evidence(str(chunk.get("text", ""))).encode("utf-8")).hexdigest()}
                    for ci, chunk in enumerate(document.get("chunks", []), 1)]}
                    for rank, document in enumerate(documents, 1)]
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            legacy = evidence_matches(item["expected_evidence"], documents, legacy_normalize)
            normalized = evidence_matches(item["expected_evidence"], documents, normalized_evidence)
            status = excluded.get(int(item["n"]), "verifiable")
            for evidence_index, expected in enumerate(item["expected_evidence"], 1):
                fact_rows.append({"query_id": item["n"], "evidence_index": evidence_index, "query": item["query"], "verification_scope": status, "expected_alternatives": json.dumps(expected if isinstance(expected, list) else [expected], ensure_ascii=False), "legacy_found": legacy[evidence_index-1], "normalized_found": normalized[evidence_index-1], "normalization_changed_match": legacy[evidence_index-1] != normalized[evidence_index-1]})
            total = len(normalized); found_legacy = sum(legacy); found_normalized = sum(normalized)
            query_rows.append({"query_id": item["n"], "query": item["query"], "verification_scope": status, "evidence_total": total, "legacy_evidence_found": found_legacy, "normalized_evidence_found": found_normalized, "legacy_all_expected_present": found_legacy == total, "normalized_all_expected_present": found_normalized == total, "legacy_query_outcome": "all" if found_legacy == total else ("partial" if found_legacy else "none"), "normalized_query_outcome": "all" if found_normalized == total else ("partial" if found_normalized else "none"), "returned_document_count": len(documents), "returned_chunk_count": sum(len(d.get("chunks", [])) for d in documents), "response_sha256": response_sha})
            print(f"captured {position}/{len(golden)}", flush=True)
    _write_csv(out / "01_evidence_fact_rows.csv", fact_rows)
    _write_csv(out / "01_evidence_query_rows.csv", query_rows)
    summary: list[dict[str, Any]] = []
    for scope, rows in [("all_dev150", query_rows), ("verifiable", [r for r in query_rows if r["verification_scope"] == "verifiable"]), ("unverifiable_evidence_extraction_failed", [r for r in query_rows if r["verification_scope"] == "evidence_extraction_failed"]), ("unverifiable_source_object_missing", [r for r in query_rows if r["verification_scope"] == "source_object_missing"]), ("unverifiable_all", [r for r in query_rows if r["verification_scope"] != "verifiable"])]:
        for method in ("legacy", "normalized"):
            found = sum(int(r[f"{method}_evidence_found"]) for r in rows); total = sum(int(r["evidence_total"]) for r in rows)
            summary.append({"scope": scope, "matching": method, "queries": len(rows), "fact_found": found, "fact_total": total, "evidence_recall": round(found / total, 6) if total else "", "all_expected_facts_present_queries": sum(r[f"{method}_query_outcome"] == "all" for r in rows), "partial_queries": sum(r[f"{method}_query_outcome"] == "partial" for r in rows), "none_queries": sum(r[f"{method}_query_outcome"] == "none" for r in rows)})
    _write_csv(out / "01_evidence_summary.csv", summary)
    (out / "01_notes.md").write_text(f"""# Dev150 evidence-recall context capture\n\n- Frozen input: `tests/frozen/golden200-2026-09-13/golden200_dev150.json` (150 cases only).\n- Baseline verified: `tests/_bench_out/operating_baseline/dev150-full-20260913T172415Z/baseline_rows.csv` has the same 150 ordered case IDs.\n- Phase-1 exclusions: 16 queries: 7 `evidence_extraction_failed`, 9 `source_object_missing`; these remain visible in `all_dev150` and are separately reported as unverifiable. `verifiable` is the primary recall denominator.\n- Retrieval: one normal deployed MCP `search` request per query, `top_k=20`; deployment exposes no more than 15 context documents. No FactChat endpoint/API/key was used.\n- `01_context_capture.jsonl` preserves the decoded returned document/chunk context exactly once per request (including returned text), source metadata, ranks, per-document chunk ordering, raw-response hash, and normalized-text hashes. It intentionally contains no request credential.\n- Legacy matching is the prior casefold + whitespace comparison. Normalized matching additionally uses NFC/HTML decoding, punctuation folding, repository Phase-1 Korean money/date forms, comma/space numeric folding, and 2026학년도/2026년도/2026년 plus 제1학기/1학기 semester equivalence. It is still substring evidence matching, so it does not establish semantic entailment or resolve table/layout extraction loss.\n- Fact rows retain fact-level numerator/denominator; query outcomes are `all`, `partial` (>0 but not all), and `none`.\n""", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
