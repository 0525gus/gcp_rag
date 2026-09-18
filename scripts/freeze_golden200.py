"""Create a deterministic bundle-grouped Golden200 dev/holdout snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260913
HOLDOUT_SIZE = 50


def _normalized_bundle(row: dict[str, Any]) -> str:
    value = str(row.get("expected_bundle") or "").casefold()
    value = re.sub(r"\.[a-z0-9]{1,8}$", "", value)
    value = re.sub(r"(?:^|[\s_\-(])\d{10,14}[\s_\-)]*$", "", value)
    value = re.sub(r"(?<!\d)(\d{4})\s*(?:학년도|년)?\s*[-_.]?\s*([12])\s*학기", r"\1 \2학기", value)
    return re.sub(r"[^0-9a-z가-힣]+", " ", value).strip()


def _tags(row: dict[str, Any]) -> set[str]:
    return set(row.get("weakness_tags") or ["legacy"])


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _choose_holdout(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    total = sum(len(rows) for rows in groups.values())
    all_rows = [row for rows in groups.values() for row in rows]
    global_tags = Counter(tag for row in all_rows for tag in _tags(row))
    target_tags = {tag: count * HOLDOUT_SIZE / total for tag, count in global_tags.items()}
    names = sorted(groups)
    best: tuple[float, list[str]] | None = None
    for trial in range(20_000):
        shuffled = names[:]
        random.Random(SEED + trial).shuffle(shuffled)
        selected: list[str] = []
        count = 0
        for name in shuffled:
            size = len(groups[name])
            if count + size <= HOLDOUT_SIZE:
                selected.append(name)
                count += size
            if count == HOLDOUT_SIZE:
                break
        if count != HOLDOUT_SIZE:
            continue
        held = [row for name in selected for row in groups[name]]
        actual = Counter(tag for row in held for tag in _tags(row))
        # Match prevalence of every weakness tag, with a small type-balance tie breaker.
        score = sum((actual[tag] - wanted) ** 2 for tag, wanted in target_tags.items())
        score += sum(
            (sum(row.get("type", "") == kind for row in held) - total_count * HOLDOUT_SIZE / total) ** 2
            for kind, total_count in Counter(row.get("type", "") for row in all_rows).items()
        ) / 100
        candidate = (score, sorted(selected))
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise RuntimeError("Could not produce an exact 50-case holdout by bundle group")
    return [row for name in best[1] for row in groups[name]]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=ROOT / "tests/golden200.json")
    parser.add_argument("--sources", type=Path, default=ROOT / "tests/_bench_out/golden200_sources.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "tests/frozen/golden200-2026-09-13")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out_dir.exists() and any(args.out_dir.iterdir()) and not args.force:
        raise SystemExit(f"Snapshot directory exists: {args.out_dir}; use --force only to replace it")
    rows = json.loads(args.golden.read_text(encoding="utf-8"))
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    if len(rows) != 200 or len({row["n"] for row in rows}) != 200:
        raise SystemExit("Golden input must have 200 unique rows")
    if {str(row["n"]) for row in rows} != set(sources):
        raise SystemExit("Source cache keys must exactly match Golden case numbers")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_normalized_bundle(row)].append(row)
    held = _choose_holdout(groups)
    holdout_n = {int(row["n"]) for row in held}
    dev = [row for row in rows if int(row["n"]) not in holdout_n]
    if len(dev) != 150 or len(held) != HOLDOUT_SIZE:
        raise RuntimeError("Invalid split cardinality")
    dev_bundles = {_normalized_bundle(row) for row in dev}
    held_bundles = {_normalized_bundle(row) for row in held}
    if dev_bundles & held_bundles:
        raise RuntimeError("A normalized bundle crosses dev/holdout boundary")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    all_path = args.out_dir / "golden200.json"
    dev_path = args.out_dir / "golden200_dev150.json"
    holdout_path = args.out_dir / "golden200_holdout50.json"
    source_path = args.out_dir / "golden200_sources.json"
    _write_json(all_path, rows)
    _write_json(dev_path, dev)
    _write_json(holdout_path, held)
    _write_json(source_path, sources)
    manifest = {
        "snapshot_id": "golden200-2026-09-13",
        "frozen_utc": datetime.now(UTC).isoformat(),
        "frozen_kst": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "seed": SEED,
        "grouping": "normalized expected_bundle; groups cannot cross splits",
        "all_cases": len(rows),
        "dev_cases": len(dev),
        "holdout_cases": len(held),
        "bundle_groups": len(groups),
        "dev_bundle_groups": len(dev_bundles),
        "holdout_bundle_groups": len(held_bundles),
        "dev_case_numbers": [int(row["n"]) for row in dev],
        "holdout_case_numbers": [int(row["n"]) for row in held],
        "tag_counts": {
            "all": Counter(tag for row in rows for tag in _tags(row)),
            "dev": Counter(tag for row in dev for tag in _tags(row)),
            "holdout": Counter(tag for row in held for tag in _tags(row)),
        },
        "files": {
            path.name: _sha256(path)
            for path in (all_path, dev_path, holdout_path, source_path)
        },
    }
    _write_json(args.out_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=dict))


if __name__ == "__main__":
    main()
