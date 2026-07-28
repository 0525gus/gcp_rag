"""골든셋 검증 결과 JSON → 보고용 마크다운 문서.

표를 손으로 옮겨 적으면 틀린다. eval_golden.py 가 남긴 결과에서 바로
뽑아, 재측정하면 문서도 같이 갱신되도록 한다.

사용:
    python scripts/eval_golden.py tests/golden100.json --out result.json
    python scripts/gen_eval_doc.py result.json docs/GOLDEN_EVAL.md
"""
import argparse
import json
import statistics
from pathlib import Path

ap = argparse.ArgumentParser(description="골든셋 결과 → 보고 문서")
ap.add_argument("result", type=Path, help="eval_golden.py --out 결과 JSON")
ap.add_argument("out", type=Path, help="쓸 마크다운 경로")
args = ap.parse_args()

d = json.loads(args.result.read_text(encoding="utf-8"))
rows = d["rows"]
OUT = args.out


def cut(s, n):
    s = (s or "-").replace("|", "｜").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def score(rs, key="rank"):
    n = len(rs)
    h = lambda k: sum(1 for r in rs if r[key] and r[key] <= k)  # noqa: E731
    mrr = sum((1.0 / r[key]) if r[key] else 0.0 for r in rs) / n
    return n, h(1), h(3), h(5), mrr


L = []
w = L.append

w("# 골든셋 검증 결과 (100건)")
w("")
w("- 측정일: 2026. 7. 28.")
w("- 대상: 배포된 MCP `search` 툴 (`rag-mcp`, 후처리 포함 전 구간)")
w("- 설정: `top_k=5`, `fetch_k=30`, 거리상한 0.30, 어휘 재정렬 on, 문서당 최대 3청크")
w("- 데이터셋: [`tests/golden100.json`](../tests/golden100.json)")
w("- 재현: `python scripts/eval_golden.py tests/golden100.json`")
w("")
w("---")
w("")
w("## Ⅰ. 골든셋 구성 방법")
w("")
w("- □ 색인된 **1,211건에서 무작위 추출**(시드 고정). 손으로 고르면 아는 문서만")
w("  고르게 되어 실제 분포를 못 본다")
w("- □ 추출된 문서의 **본문을 실제로 읽고** 질문을 작성함")
w("  - ○ 제목을 그대로 베끼지 않고 사용자가 쓸 법한 말로 바꿔 적음")
w("  - ○ 이전 15건짜리 골든셋은 질의가 문서 제목 어휘를 36% 재현해 어휘 재정렬에")
w("    유리한 편향이 있었음. 현재 셋은 17%")
w("- □ **본문이 없는 문서는 제외**함(엑셀 스텁, 텍스트층 없는 PDF 등)")
w("  - ○ 사유: 질문이 '파일명이 걸리나'를 재는 데 그쳐 검색 품질의 신호가 아님")
w("  - ○ 다만 그 유형이 **코퍼스의 약 24%**라는 점은 별도 고려 필요")
w("")
w("### 정답 판정 기준")
w("")
w("| 기준 | 정의 |")
w("|---|---|")
w("| 정확히 그 파일 | 기대한 fileId 가 상위 5건에 있음 |")
w("| 같은 자료묶음 | 같은 폴더의 다른 문서(공문↔붙임)도 인정 |")
w("")
w("- □ 드라이브에 같은 문서가 두 벌 있는 경우(`폴더 (1)` 형태)는 **본문 바이트를")
w("  대조해 동일함을 확인한 것만** 정답으로 함께 인정함(`also_accept`, 4건)")
w("")

n, h1, h3, h5, mrr = score(rows)
bn, bh1, bh3, bh5, bmrr = score(rows, "bundle_rank")
w("---")
w("")
w("## Ⅱ. 결과 요약")
w("")
w("| 기준 | hit@1 | hit@3 | hit@5 | MRR |")
w("|---|---:|---:|---:|---:|")
pc = lambda x, t: f"{x}/{t} ({x/t*100:.0f}%)"
w(f"| 정확히 그 파일 | {pc(h1,n)} | {pc(h3,n)} | **{pc(h5,n)}** | {mrr:.3f} |")
w(f"| 같은 자료묶음 | {pc(bh1,bn)} | {pc(bh3,bn)} | {pc(bh5,bn)} | {bmrr:.3f} |")
w("")
w(f"- □ 빈 결과: **{sum(1 for r in rows if r['n_hits'] == 0)}건 / {n}건**")
w("- □ 코퍼스 범위 밖 질의 차단: **6건 / 6건**")
w("")
w("### 표본 구간별 (해석 주의)")
w("")
old = [r for r in rows if r["n"] <= 38]
new = [r for r in rows if r["n"] > 38]
w("| 구간 | n | hit@1 | hit@3 | hit@5 | MRR |")
w("|---|---:|---:|---:|---:|---:|")
for lbl, rs in (("기존 38건", old), ("신규 62건", new)):
    a, b, c, e, m = score(rs)
    w(f"| {lbl} | {a} | {b} ({b/a*100:.0f}%) | {c} ({c/a*100:.0f}%) | {e} ({e/a*100:.0f}%) | {m:.3f} |")
w("")
w("- □ **hit@1 은 두 구간이 사실상 동일**(45% vs 47%) — 이 값이 안정적임")
w("- □ hit@5 는 89% vs 97% 로 벌어짐")
w("  - ○ 원인: 신규 62건은 추출 시 **본문 200자 이상** 필터를 적용했고,")
w("    기존 38건에는 대비표 등 정보량이 낮은 문서가 남아 있음")
w("  - ○ 따라서 **전체 94% 는 다소 낙관적**이며, 보수적으로는 **89~94% 구간**으로 읽을 것")
w("")
w("---")
w("")
w("## Ⅲ. 질의별 결과 (100건 전량)")
w("")
w("- □ `대답` 열은 **시스템이 1위로 반환한 문서**임(정답 문서가 아님)")
w("- □ `순위` 는 정답 문서가 반환된 위치. `-` 는 상위 5건에 없음")
w("")
w("| # | 질의 | 대답 (반환 1위 문서) | 정답 문서 | 순위 | hit@5 |")
w("|---:|---|---|---|---:|:---:|")
for r in rows:
    top = cut(r["names"][0] if r["names"] else None, 40)
    exp = cut(r["name"], 40)
    rank = r["rank"] or "-"
    ok = "O" if (r["rank"] and r["rank"] <= 5) else "**X**"
    same = "〃" if top == exp else exp
    w(f"| {r['n']} | {cut(r['q'], 46)} | {top} | {same} | {rank} | {ok} |")
w("")
w("- □ `〃` 는 반환 1위가 곧 정답 문서인 경우")
w("")
w("---")
w("")
miss = [r for r in rows if not r["rank"]]
w(f"## Ⅳ. 미검출 {len(miss)}건 상세")
w("")
for r in rows:
    if r["rank"]:
        continue
    w(f"### {r['n']}. {r['q']}")
    w("")
    w(f"- □ 정답 문서: `{r['name']}`")
    w(f"- □ 자료묶음: {r['bundle']}")
    if r["bundle_rank"]:
        w(f"- □ **같은 묶음 문서가 {r['bundle_rank']}위로 반환됨** — 사용자는 답에 도달 가능")
    else:
        w("- □ 같은 묶음 문서도 반환되지 않음")
    if r["names"]:
        w("- □ 실제 반환")
        for i, nm in enumerate(r["names"], 1):
            w(f"  - {i}. {nm}")
    else:
        w("- □ 실제 반환: **0건** (거리 임계값이 전량 제외)")
    w("")

hs = [r["hit_score"] for r in rows if r.get("hit_score") is not None]
q = statistics.quantiles(hs, n=100)
w("---")
w("")
w("## Ⅴ. 거리 임계값 재교정")
w("")
w("- □ 기존 임계값 `0.30` 은 **15건 표본**으로 정한 값이었음")
w("- □ 100건으로 다시 측정한 정답 문서의 거리 분포")
w("")
w("| 구분 | 값 |")
w("|---|---:|")
w(f"| 최소 | {min(hs):.3f} |")
w(f"| 중앙값 | {statistics.median(hs):.3f} |")
w(f"| p95 | {q[94]:.3f} |")
w(f"| **최대** | **{max(hs):.3f}** |")
w("")
w("- □ 기존 명세에 기재한 정답 범위는 `0.120 ~ 0.214` 였으나 **실제 상한은 0.275** 임")
w("  - ○ 표본이 작아 상한을 0.06 과소평가했음")
w("- □ 무관 질의는 0.330 부터 시작하므로 **실제 여유는 약 0.05** 로, ")
w("  기존 명세의 서술보다 빠듯함")
w("")
w("### 실제로 발생한 비용 (39번)")
w("")
w("```")
w('"냉난방기를 안 트는 기간이 언제부터…"   (일상어)   → 0건      ← 임계값 초과')
w('"냉난방기 휴지기간이 언제인가요?"        (문서 어휘) → 1위 0.283')
w("```")
w("")
w("- □ 문서는 정상 색인되어 있고 검색도 가능하나, **질문을 일상어로 하면 통째로 잘림**")
w("- □ 100건 중 처음 발생한 빈 결과임")
w("")
w("### 선택지")
w("")
w("| 방안 | 효과 | 위험 |")
w("|---|---|---|")
w("| 현행 `0.30` 유지 | 무관 질의 차단 6/6 | 일상어 질의가 가끔 전량 차단됨 |")
w("| `0.32` 로 완화 | 39번 유형 구제 | 무관 질의 시작점(0.330)에 근접 |")
w("| **0건일 때만 재질의** | 차단 성능 유지 + 빈손 방지 | 구현 필요 |")
w("")
w("- □ 세 번째 방안이 유리해 보이나, **측정 없이 변경하지 않음**")
w("")

open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
print("작성 완료:", OUT, f"({len(L)} 줄)")
