# 벤치 결과 (원자료)

> 정리된 보고 문서는 [`docs/PARSER_BENCH.md`](../../docs/PARSER_BENCH.md) 참조.
> 이 디렉터리는 그 근거가 되는 원자료다.

- 측정일: 2026-07-22
- 대상: `tests/2026_문서접수_test` HWP/HWPX **135건**
- 결과 요약
  - rhwp   : 100% 성공, avg **20.2ms**, 표 인식 2.47개/건 → **채택**
  - kordoc : 100% 성공, avg 341.7ms, 표 인식 0.58개/건 → 폐기
  - 품질게이트 133/135 통과, 탈락 2건은 이미지 비중 큰 공문(오탐)

## 파일

| 파일 | 내용 |
|---|---|
| `bench_summary.json` | 엔진별 집계 |
| `bench_rows.csv` | 135건 × 2엔진 개별 결과 |
| `gate_fail_samples/` | 품질게이트 탈락 샘플 |
| `rhwp_sample.md` / `kordoc_sample.md` | 동일 문서의 엔진별 변환 결과 비교 |

## 재현

```bash
python scripts/bench_hwp_corpus.py <코퍼스 디렉터리>
```

현행 운영 설정: `QG_DENSITY_THRESHOLD=0.0005`, `QG_MODE=log`
