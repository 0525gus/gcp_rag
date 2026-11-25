from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dept_config import build_env
from shared.search_response import response_documents

DEFAULT_TOP_K = 5


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def normalize_golden(item: dict[str, Any], index: int) -> dict[str, Any]:
    row = dict(item)
    row["n"] = row.get("n", index)
    row["query"] = row.get("query", row.get("q"))
    row["expected_file"] = _as_list(row.get("expected_file", row.get("file_id")))
    row["expected_bundle"] = _as_list(row.get("expected_bundle", row.get("bundle")))
    row["expected_evidence"] = row.get("expected_evidence", [])
    if not row["query"]:
        raise ValueError(f"golden row {row['n']}: query is required")
    if not row["expected_file"]:
        raise ValueError(f"golden row {row['n']}: expected_file is required")
    if not isinstance(row["expected_evidence"], list):
        raise TypeError(f"golden row {row['n']}: expected_evidence must be a list")
    return row


_WS = re.compile(r"\s+")
_MONEY = re.compile(r"(?<!\d)(\d[\d, ]*)\s*(만원|천원|원)(?![가-힣])")
_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[ \t]*(?:년|[./-])[ \t]*"
    r"(\d{1,2})[ \t]*(?:월|[./-])[ \t]*(\d{1,2})(?:일)?"
)
_SEMESTER = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:학년도|년도|년)?\s*(?:제\s*)?([12])\s*학기"
)
_BARE_SEMESTER = re.compile(r"(?<![\d가-힣])(?:제\s*)?([12])\s*학기")
_GROUPED_NUMBER_WITH_UNIT = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3})+)(?=[ \t]*(?:명|건|회|개|쪽|페이지|%))"
)


def normalize_evidence_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(value or "")).casefold()

    def date(match: re.Match[str]) -> str:
        return (
            f" date{int(match.group(1)):04d}"
            f"{int(match.group(2)):02d}{int(match.group(3)):02d} "
        )

    def money(match: re.Match[str]) -> str:
        amount = int(re.sub(r"[, ]", "", match.group(1)))
        multiplier = {"만원": 10_000, "천원": 1_000, "원": 1}[match.group(2)]
        return f" moneywon{amount * multiplier} "

    def semester(match: re.Match[str]) -> str:
        return f" semester{int(match.group(1)):04d}{match.group(2)} "

    text = _DATE.sub(date, text)
    text = _MONEY.sub(money, text)
    text = _SEMESTER.sub(semester, text)
    text = _BARE_SEMESTER.sub(lambda match: f" semester{match.group(1)} ", text)
    text = _GROUPED_NUMBER_WITH_UNIT.sub(
        lambda match: match.group(1).replace(",", ""), text
    )
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return _WS.sub(" ", text.replace("_", " ")).strip()


def evidence_matches(expected: list[Any], documents: list[dict[str, Any]]) -> list[bool]:
    corpus = normalize_evidence_text("\n".join(
        str(chunk.get("text", ""))
        for document in documents
        for chunk in document.get("chunks", [])
    ))
    matches: list[bool] = []
    for unit in expected:
        alternatives = unit if isinstance(unit, list) else [unit]
        matches.append(any(
            normalize_evidence_text(str(value)) in corpus
            for value in alternatives
            if normalize_evidence_text(str(value))
        ))
    return matches


def evidence_recall(expected: list[Any], documents: list[dict[str, Any]]) -> tuple[int, int]:
    matches = evidence_matches(expected, documents)
    return sum(matches), len(matches)


def latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def percentile(p: float) -> float:
        return ordered[round((len(ordered) - 1) * p)]

    return {
        "mean_ms": round(statistics.fmean(values), 2),
        "median_ms": round(statistics.median(values), 2),
        "p95_ms": round(percentile(0.95), 2),
        "max_ms": round(max(values), 2),
    }


def call_search_result(url: str, key: str, query: str, top_k: int) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "top_k": top_k}},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    data_lines = [
        line[5:].lstrip()
        for line in raw.splitlines()
        if line.lstrip().startswith("data:")
    ]
    if data_lines:
        raw = "".join(data_lines)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid MCP JSON bytes={len(raw)} data_lines={len(data_lines)} "
            f"tail={raw[-120:]!r}"
        ) from exc
    result = payload.get("result", {})
    documents = response_documents(result)
    for document in documents:
        for chunk in document.get("chunks", []):
            value = chunk.get("text", "")
            if isinstance(value, str) and value.startswith("zlib64:"):
                chunk["text"] = zlib.decompress(base64.b64decode(value[7:])).decode("utf-8")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "documents" in structured:
        return structured
    return {"documents": documents}


def call_search(url: str, key: str, query: str, top_k: int) -> list[dict[str, Any]]:
    return call_search_result(url, key, query, top_k)["documents"]


def _rank(seq: list[Any], want: Any) -> int | None:
    return next((i for i, v in enumerate(seq, 1) if v is not None and v == want), None)


def score(rows: list[dict], key: str) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {}

    def hit(k: int) -> int:
        return sum(1 for r in rows if r[key] and r[key] <= k)

    mrr = sum((1.0 / r[key]) if r[key] else 0.0 for r in rows) / n
    return {
        "n": n,
        "hit@1": hit(1),
        "hit@3": hit(3),
        "hit@5": hit(5),
        "hit@1_rate": round(hit(1) / n, 3),
        "hit@3_rate": round(hit(3) / n, 3),
        "hit@5_rate": round(hit(5) / n, 3),
        "mrr": round(mrr, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋으로 검색 품질 측정")
    ap.add_argument(
        "golden", type=Path,
        help="골든셋 JSON (query/expected_file/expected_bundle/expected_evidence)",
    )
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--out", type=Path, help="상세 결과를 쓸 JSON 경로")
    ap.add_argument("--sleep", type=float, default=0.3, help="질의 사이 간격(초)")
    ap.add_argument("--dept", help="MCP 키를 이 학과 설정에서 가져온다")
    ap.add_argument("--audience", default="staff", choices=("staff", "student"))
    args = ap.parse_args()

    url = os.environ.get("MCP_URL", "").strip()
    if not url:
        print("MCP_URL 이 없습니다 (예: https://<서비스>/mcp)", file=sys.stderr)
        return 2
    if args.dept:
        key = build_env(args.dept, args.audience)["MCP_API_KEY"]
    else:
        key = os.environ.get("MCP_API_KEY", "").strip()

    raw_golden = json.loads(args.golden.read_text(encoding="utf-8"))
    golden = [normalize_golden(item, i) for i, item in enumerate(raw_golden, 1)]
    rows: list[dict[str, Any]] = []

    for g in golden:
        hits: list[dict[str, Any]] = []
        started = time.perf_counter()
        for attempt in range(3):
            try:
                hits = call_search(url, key, g["query"], args.top_k)
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    print(f"\n[{g.get('n')}] 실패: {exc}", file=sys.stderr)
                else:
                    time.sleep(2 * (attempt + 1))

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        sources = [h.get("source", {}) for h in hits]
        ids = [s.get("fileId") for s in sources]
        names = [(s.get("name") or "").strip().lower() for s in sources]
        bundles = [(s.get("bundle") or "").strip() for s in sources]

        accept = {*g["expected_file"], *g.get("also_accept", [])}
        strict = next((i for i, f in enumerate(ids, 1) if f in accept), None)

        scores = [h.get("score", (h.get("chunks") or [{}])[0].get("score")) for h in hits]
        hit_score = next(
            (s for s, f in zip(scores, ids) if f in
             accept), None
        )

        evidence_found, evidence_total = evidence_recall(g["expected_evidence"], hits)

        rows.append({
            **g,
            "latency_ms": latency_ms,
            "evidence_found": evidence_found,
            "evidence_total": evidence_total,
            "evidence_recall": (
                round(evidence_found / evidence_total, 3) if evidence_total else None
            ),
            "top_score": scores[0] if scores else None,
            "hit_score": hit_score,
            "rank": strict,
            "same_doc_rank": _rank(names, (g.get("name") or "").strip().lower()),
            "bundle_rank": next(
                (i for i, value in enumerate(bundles, 1) if value in g["expected_bundle"]),
                None,
            ),
            "n_hits": len(hits),
            "distinct": len({n for n in names if n}),
            "ranked": ids,
            "names": [s.get("name") for s in sources],
        })
        sys.stderr.write(f"{g.get('n')}:{rows[-1]['rank'] or '-'} ")
        time.sleep(args.sleep)
    sys.stderr.write("\n\n")

    slots = sum(r["n_hits"] for r in rows)
    waste = sum(r["n_hits"] - r["distinct"] for r in rows)
    out = {
        "top_k": args.top_k,
        "strict": score(rows, "rank"),
        "same_doc": score(rows, "same_doc_rank"),
        "bundle": score(rows, "bundle_rank"),
        "evidence_recall": {
            "found": sum(r["evidence_found"] for r in rows),
            "total": sum(r["evidence_total"] for r in rows),
            "rate": round(
                sum(r["evidence_found"] for r in rows)
                / sum(r["evidence_total"] for r in rows), 3
            ) if sum(r["evidence_total"] for r in rows) else None,
            "annotated_queries": sum(r["evidence_total"] > 0 for r in rows),
        },
        "latency": latency_summary([r["latency_ms"] for r in rows]),
        "empty_results": sum(1 for r in rows if r["n_hits"] == 0),
        "duplicate_slots": {"slots": slots, "wasted": waste},
        "by_type": {
            t: score([r for r in rows if r.get("type", "?") == t], "same_doc_rank")
            for t in sorted({r.get("type", "?") for r in rows})
        },
        "rows": rows,
    }

    label = {"strict": "정확히 그 파일", "same_doc": "같은 문서(사본포함)",
             "bundle": "같은 자료묶음"}
    print(f"골든 {len(rows)}건 / top_k={args.top_k}\n")
    for k, name in label.items():
        s = out[k]
        if not s:
            continue
        print(f"  {name:<18} hit@1 {s['hit@1']:>2}/{s['n']}  "
              f"hit@3 {s['hit@3']:>2}/{s['n']}  hit@5 {s['hit@5']:>2}/{s['n']}  "
              f"MRR {s['mrr']:.3f}")
    print(f"\n  빈 결과 {out['empty_results']}건 | "
          f"상위 {slots}칸 중 {waste}칸이 중복 사본")
    ev = out["evidence_recall"]
    ev_rate = f"{ev['rate']:.3f}" if ev["rate"] is not None else "N/A (미주석)"
    print(f"  Evidence Recall {ev_rate} ({ev['found']}/{ev['total']})")
    latency = out["latency"]
    print(f"  Latency mean {latency['mean_ms']:.0f}ms | median {latency['median_ms']:.0f}ms | "
          f"p95 {latency['p95_ms']:.0f}ms | max {latency['max_ms']:.0f}ms")

    print("\n  유형별 (같은 문서 기준)")
    for t, s in out["by_type"].items():
        print(f"    {t:<6} n={s['n']:<3} hit@1 {s['hit@1']}/{s['n']}  "
              f"hit@5 {s['hit@5']}/{s['n']}")

    misses = [r for r in rows if not r["same_doc_rank"]]
    if misses:
        print(f"\n  상위 {args.top_k} 안에 못 들어온 {len(misses)}건")
        for r in misses:
            tag = (f"묶음 {r['bundle_rank']}위" if r["bundle_rank"] else "완전 실패")
            print(f"    [{r.get('n'):>2}] {r.get('type', '?'):<5} "
                  f"{tag:<10} {r['query'][:44]}")

    if args.out:
        args.out.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n상세: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
