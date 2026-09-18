# Golden200 weakness-focused expansion — 2026-09-13

## Result

The audited Golden100 was retained as cases 1–100. Cases 101–200 add 100 new
staff-corpus questions selected around weaknesses observed in the 28 non-correct
baseline cases. No FactChat endpoint was used.

| Generation target | New cases |
|---|---:|
| Period/year/semester conflict | 20 |
| Round/edition/attachment/form conflict | 15 |
| Revision conflict | 15 |
| Numeric/table lookup | 15 |
| Similar title/bundle | 15 |
| Multiple conditions | 10 |
| Evidence distributed across source units | 10 |
| **Total** | **100** |

Because one question can exercise multiple weaknesses, the final rows also contain
verified `weakness_tags`. Across the new set the overlapping tags occur as follows:

| Verified tag | Cases |
|---|---:|
| Period conflict | 72 |
| Numeric/table | 60 |
| Multiple conditions | 89 |
| Distributed evidence | 46 |
| Similar title/bundle | 15 |
| Revision conflict | 12 |
| Sequence conflict | 8 |

## Diversity and grounding checks

- 200 rows and 200 unique questions
- 200 unique source file IDs
- 100/100 new rows use distinct exact and normalized bundles
- No normalized bundle overlap between the original and new 100 rows
- Every row has an expected answer, 1–5 verbatim evidence units, required facts,
  and an explicit forbidden-facts array
- Every evidence unit occurs exactly in the cached authoritative source
- Placeholder dates and values such as `00월 00일` are rejected
- New question length: 19–127 characters
- 85/100 new rows have an explicit year/version label
- 96 new rows were independently audited by Gemini; four edge cases were manually
  corrected directly against their authoritative source

The `generation_target` field records the intended quota used for source selection.
The `weakness_tags` field records properties actually visible in the final question and
label; tags intentionally overlap.

## Artifacts

- `tests/golden200.json`
- `tests/_bench_out/golden200_sources.json`
- `scripts/expand_golden200.py`
- `scripts/finalize_golden200.py`
