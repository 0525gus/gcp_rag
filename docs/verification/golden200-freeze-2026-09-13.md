# Golden200 dev/holdout snapshot — 2026-09-13

## Frozen split

Golden200 was frozen before the next retrieval experiments.

| Split | Cases | Purpose |
|---|---:|---|
| Dev | 150 | Failure analysis and all candidate/ranking/evidence tuning |
| Holdout | 50 | One final evaluation after settings are fixed |

The split is deterministic (`seed=20260913`) and groups documents by normalized
`expected_bundle`: a bundle can never appear in both sets. This produced 182 bundle
groups: 134 dev groups and 48 holdout groups.

The holdout preserves the overall approximate weakness mix. It includes 25 original
Golden100 cases and 25 weakness-focused new cases.

## Freeze rules

- Do not tune retrieval, ranking, prompt, or evidence-packing parameters using the
  50 holdout questions or their labels.
- Run all alternatives against `golden200_dev150.json` first.
- Select exactly one configuration using Dev only.
- Run that fixed configuration on holdout once; do not make a further change from
  that result.
- Verify the SHA-256 manifest before every benchmark and after copying artifacts.

## Artifacts

- `tests/frozen/golden200-2026-09-13/golden200.json`
- `tests/frozen/golden200-2026-09-13/golden200_dev150.json`
- `tests/frozen/golden200-2026-09-13/golden200_holdout50.json`
- `tests/frozen/golden200-2026-09-13/golden200_sources.json`
- `tests/frozen/golden200-2026-09-13/manifest.json`
- `scripts/freeze_golden200.py`

The freeze integrity test recomputes each manifest hash and verifies the case split.
