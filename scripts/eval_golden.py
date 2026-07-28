"""골든셋 검색 품질 측정 — 배포된 MCP 서버의 search 툴을 그대로 때린다.

Vertex retrieveContexts 를 직접 부르면 거리 임계값·어휘 재정렬·청크 병합이
전부 빠진 날것을 재게 된다. FactChat 이 실제로 받는 건 MCP 응답이므로
그쪽을 잰다.

사용:
    export MCP_URL=https://<서비스>/mcp
    export MCP_API_KEY=<키>
    python scripts/eval_golden.py tests/golden50.json
    python scripts/eval_golden.py tests/golden50.json --top-k 10 --out result.json

지표를 세 가지로 나눠 내는 이유:

  정확히 그 파일  기대한 fileId 가 상위 k 에 있었나
  같은 문서      파일명이 같은 다른 사본도 인정 — 드라이브에 `폴더 (1)` 형태로
                 같은 문서가 두 벌 있어, 사본이 걸려도 사용자는 답을 받는다
  같은 자료묶음   같은 폴더의 다른 문서도 인정 — 공문 1건에 붙임 여러 개가
                 한 폴더로 들어오므로 붙임 대신 공문이 걸려도 답에 도달한다

첫 번째만 보면 과소평가, 세 번째만 보면 과대평가다. 셋을 같이 읽을 것.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TOP_K = 5


def call_search(url: str, key: str, query: str, top_k: int) -> list[dict[str, Any]]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "top_k": top_k}},
    }
    headers = {
        "Content-Type": "application/json",
        # streamable-http 는 SSE 로 돌려줄 수 있어 둘 다 받는다고 알린다
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
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            raw = line[5:].strip()
            break

    hits: list[dict[str, Any]] = []
    for block in json.loads(raw).get("result", {}).get("content", []):
        if block.get("type") != "text":
            continue
        try:
            hits.append(json.loads(block["text"]))
        except json.JSONDecodeError:
            # 툴이 에러 문자열을 그대로 실어 보낸 경우 — 히트로 세지 않는다
            pass
    return hits


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
        "hit@5_rate": round(hit(5) / n, 3),
        "mrr": round(mrr, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋으로 검색 품질 측정")
    ap.add_argument("golden", type=Path, help="골든셋 JSON (q/file_id/name/bundle/type)")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--out", type=Path, help="상세 결과를 쓸 JSON 경로")
    ap.add_argument("--sleep", type=float, default=0.3, help="질의 사이 간격(초)")
    args = ap.parse_args()

    url = os.environ.get("MCP_URL", "").strip()
    if not url:
        print("MCP_URL 이 없습니다 (예: https://<서비스>/mcp)", file=sys.stderr)
        return 2
    key = os.environ.get("MCP_API_KEY", "").strip()

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []

    for g in golden:
        hits: list[dict[str, Any]] = []
        for attempt in range(3):
            try:
                hits = call_search(url, key, g["q"], args.top_k)
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    print(f"\n[{g.get('n')}] 실패: {exc}", file=sys.stderr)
                else:
                    time.sleep(2 * (attempt + 1))

        sources = [h.get("source", {}) for h in hits]
        ids = [s.get("fileId") for s in sources]
        names = [(s.get("name") or "").strip().lower() for s in sources]
        bundles = [(s.get("bundle") or "").strip() for s in sources]

        rows.append({
            **g,
            "rank": _rank(ids, g["file_id"]),
            "same_doc_rank": _rank(names, (g.get("name") or "").strip().lower()),
            "bundle_rank": _rank(bundles, (g.get("bundle") or "").strip())
            if g.get("bundle") else None,
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
        "empty_results": sum(1 for r in rows if r["n_hits"] == 0),
        "duplicate_slots": {"slots": slots, "wasted": waste},
        "by_type": {
            t: score([r for r in rows if r["type"] == t], "same_doc_rank")
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

    print("\n  유형별 (같은 문서 기준)")
    for t, s in out["by_type"].items():
        print(f"    {t:<6} n={s['n']:<3} hit@1 {s['hit@1']}/{s['n']}  "
              f"hit@5 {s['hit@5']}/{s['n']}")

    misses = [r for r in rows if not r["same_doc_rank"]]
    if misses:
        print(f"\n  상위 {args.top_k} 안에 못 들어온 {len(misses)}건")
        for r in misses:
            tag = (f"묶음 {r['bundle_rank']}위" if r["bundle_rank"] else "완전 실패")
            print(f"    [{r.get('n'):>2}] {r['type']:<5} {tag:<10} {r['q'][:44]}")

    if args.out:
        args.out.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n상세: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
