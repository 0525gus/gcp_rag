"""xlsx → 마크다운 변환률을 실제 코퍼스로 잰다.

DEV_SPEC "1-1. XLSX 셀 → 마크다운 표" 의 수치를 재현한다.
셀 내용은 출력하지 않는다 — 명단류가 섞여 있어 집계만 낸다.

사용:
    # GCS 에서 받아서
    gcloud storage cp "gs://<normalized-bucket>/normalized/*.xlsx" ./xlsx_sample/
    python scripts/bench_xlsx_md.py ./xlsx_sample
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.xlsx_md import XlsxParseError, xlsx_to_markdown  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", help="xlsx 파일이 든 디렉터리")
    args = ap.parse_args()

    # rglob — 실코퍼스는 폴더 구조를 그대로 내려받아 하위 디렉터리에 흩어져 있다.
    # glob 이면 조용히 0건이 나와 "문제 없음"으로 오해한다.
    files = sorted(Path(args.corpus).rglob("*.xlsx"))
    if not files:
        print(f"xlsx 없음: {args.corpus}", file=sys.stderr)
        return 1

    sizes: list[int] = []
    times: list[float] = []
    fails: collections.Counter[str] = collections.Counter()

    for f in files:
        data = f.read_bytes()
        t0 = time.perf_counter()
        try:
            md = xlsx_to_markdown(data)
        except XlsxParseError as exc:
            fails[str(exc)[:60]] += 1
            continue
        times.append((time.perf_counter() - t0) * 1000)
        sizes.append(len(md))

    print(f"전체 {len(files)}건")
    print(f"  변환 성공 {len(sizes)}건")
    print(f"  변환 실패 {sum(fails.values())}건")
    for reason, n in fails.most_common():
        print(f"    {n:4d}  {reason}")

    if sizes:
        print(
            f"\n출력 md 크기 min/중앙/max = "
            f"{min(sizes):,} / {int(statistics.median(sizes)):,} / {max(sizes):,} 자"
        )
        # 한글 UTF-8 3바이트 기준 상한 근사
        print(f"  최대 파일 예상 바이트: {max(sizes) * 3 / 1024 / 1024:.2f} MB (한도 10MB)")
        print(f"속도 중앙값 {statistics.median(times):.1f}ms / 전량 {sum(times) / 1000:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
