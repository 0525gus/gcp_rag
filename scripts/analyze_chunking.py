"""청크 크기 실측 — 표/문서가 청크 경계에서 얼마나 잘리는지 센다.

검색 품질을 직접 재려면 정답이 있는 질의 세트와 살아있는 코퍼스가 필요하다.
대신 여기서는 **결정을 좌우하는 결정론적 변수**만 측정한다.

  - 표가 청크 경계에 걸리는 비율  ← 잘리면 검색 시 표의 절반만 걸린다
  - 문서 하나가 청크 하나에 들어가는 비율
  - 문서당 청크 수 분포

사용:
    python scripts/analyze_chunking.py <코퍼스_디렉터리>
    python scripts/analyze_chunking.py <디렉터리> --chars-per-token 1.8

주의:
  * Vertex 청킹은 토큰 기준인데 여기서는 문자 수 / chars-per-token 으로 근사한다.
    기본값은 운영 코퍼스 실측치다(아래).
  * 고정 크기 분할을 가정한다. Vertex 가 문단 경계를 존중한다면 실제 분할 지점은
    다를 수 있으므로, 절대값보다 **크기 간 상대 비교**로 읽는 게 맞다.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CANDIDATES = (512, 768, 1024, 1536, 2048, 3072)

# 운영 코퍼스에서 실제로 돌아온 청크 182개의 문자 길이 중앙값 981 / 청크 설정
# 1024 = 0.96. 한글 비중 50% 인 공문서 기준이다.
#
# 이전 기본값은 2.0 이었는데, 그 값으로 뽑은 표를 근거로 청크를 1024 로 정했다
# (a549208). 실제 배율이 절반이라 그 표의 "1024" 행은 현실의 2048 에 해당했고,
# 1024 의 실제 표 절단율은 6% 가 아니라 22% 였다. 가정을 바꾸면 결론이 바뀌는
# 종류의 스크립트이므로 기본값은 실측치로 둔다.
DEFAULT_CHARS_PER_TOKEN = 0.96


@dataclass
class DocStats:
    name: str
    chars: int
    tables: list[tuple[int, int]] = field(default_factory=list)  # (시작, 끝) 문자 오프셋


def find_gfm_tables(md: str) -> list[tuple[int, int]]:
    """GFM 표 블록의 (시작, 끝) 문자 오프셋. 연속된 '|' 줄을 하나로 묶는다."""
    spans: list[tuple[int, int]] = []
    offset = 0
    start: int | None = None
    for line in md.splitlines(keepends=True):
        stripped = line.strip()
        is_row = stripped.startswith("|") and stripped.endswith("|")
        if is_row and start is None:
            start = offset
        elif not is_row and start is not None:
            spans.append((start, offset))
            start = None
        offset += len(line)
    if start is not None:
        spans.append((start, offset))
    # 한 줄짜리는 표가 아니라 오탐
    return [(s, e) for s, e in spans if md[s:e].count("\n") >= 2]


def load_docs(root: Path) -> list[DocStats]:
    from services.parser.cleanup import cleanup_markdown
    from services.parser.engine import can_parse, parse_document_bytes

    docs: list[DocStats] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".md":
                md = path.read_text(encoding="utf-8")
            elif suffix in (".hwp", ".hwpx"):
                if not can_parse(path.name):
                    continue
                out = parse_document_bytes(path.read_bytes(), filename=path.name)
                md = cleanup_markdown(out.markdown)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        docs.append(DocStats(name=path.name, chars=len(md), tables=find_gfm_tables(md)))
    return docs


def evaluate(docs: list[DocStats], chunk_tokens: int, chars_per_token: float) -> dict:
    """고정 크기 분할을 가정하고 표 절단·문서 분할을 센다."""
    span = max(1, int(chunk_tokens * chars_per_token))
    total_tables = 0
    split_tables = 0
    single_chunk_docs = 0
    chunk_counts: list[int] = []

    for doc in docs:
        n_chunks = max(1, -(-doc.chars // span))  # ceil
        chunk_counts.append(n_chunks)
        if n_chunks == 1:
            single_chunk_docs += 1
        for start, end in doc.tables:
            total_tables += 1
            # 표 내부에 청크 경계가 지나가면 잘린 것
            if start // span != (end - 1) // span:
                split_tables += 1

    return {
        "chunk_tokens": chunk_tokens,
        "span_chars": span,
        "tables": total_tables,
        "split": split_tables,
        "split_pct": (split_tables / total_tables * 100) if total_tables else 0.0,
        "single_doc_pct": (single_chunk_docs / len(docs) * 100) if docs else 0.0,
        "avg_chunks": statistics.mean(chunk_counts) if chunk_counts else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="청크 크기별 표 절단율 실측")
    ap.add_argument("corpus", type=Path, help=".md / .hwp / .hwpx 가 있는 디렉터리")
    ap.add_argument("--chars-per-token", type=float, default=DEFAULT_CHARS_PER_TOKEN)
    args = ap.parse_args()

    if not args.corpus.is_dir():
        print(f"디렉터리가 아님: {args.corpus}", file=sys.stderr)
        return 1

    docs = load_docs(args.corpus)
    if not docs:
        print("분석할 문서를 찾지 못했습니다 (.md/.hwp/.hwpx)", file=sys.stderr)
        return 1

    chars = [d.chars for d in docs]
    n_tables = sum(len(d.tables) for d in docs)
    print(f"문서 {len(docs)}건 | 표 {n_tables}개 | chars/token={args.chars_per_token}")
    print(
        f"문서 길이(자): 중앙값 {int(statistics.median(chars))} "
        f"| 평균 {int(statistics.mean(chars))} "
        f"| 최소 {min(chars)} | 최대 {max(chars)}"
    )
    print()
    print(f"{'chunk':>6} {'≈문자':>7} {'표절단':>12} {'1청크문서':>9} {'평균청크':>8}")
    print("-" * 50)
    rows = [evaluate(docs, c, args.chars_per_token) for c in CANDIDATES]
    for r in rows:
        split = f"{r['split']}/{r['tables']} ({r['split_pct']:.0f}%)"
        print(
            f"{r['chunk_tokens']:>6} {r['span_chars']:>7} {split:>12} "
            f"{r['single_doc_pct']:>8.0f}% {r['avg_chunks']:>8.1f}"
        )

    print()
    if n_tables:
        best = min(rows, key=lambda r: (r["split_pct"], r["chunk_tokens"]))
        print(
            f"표 절단이 가장 적은 후보: chunk_size={best['chunk_tokens']} "
            f"(절단 {best['split_pct']:.0f}%)"
        )
        print("표 절단율이 같다면 작은 쪽이 검색 정밀도에 유리합니다.")
    else:
        print("표가 없어 절단 지표를 못 냅니다 — 문서 길이 분포로 판단하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
