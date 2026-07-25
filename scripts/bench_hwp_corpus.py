#!/usr/bin/env python3
"""실데이터 HWP/HWPX: rhwp-python 품질·성능 검증.

Usage:
  PYTHONPATH=. python scripts/bench_hwp_corpus.py tests/2026_문서접수_test
"""

from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.parser.cleanup import cleanup_markdown
from services.parser.quality_gate import ParseMetrics, evaluate_quality
from services.parser.engine import can_parse, parse_document_bytes
from shared.config import Settings


@dataclass
class Row:
    path: str
    name: str
    ext: str
    size_bytes: int
    engine: str
    ok: bool
    ms: float
    md_chars: int = 0
    table_count: int = 0
    density: float = 0.0
    gate_pass: bool = False
    gate_reasons: list[str] = field(default_factory=list)
    error: str = ""
    preview: str = ""


def _settings() -> Settings:
    return Settings(
        gcp_project_id="bench",
        gcs_raw_bucket="b",
        gcs_normalized_bucket="b",
        rag_corpus_name="projects/b/locations/asia-northeast3/ragCorpora/c",
        qg_density_threshold=0.0005,
        qg_mode="log",
    )


def iter_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".hwp", ".hwpx"}
    )


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/2026_문서접수_test")
    if not root.is_absolute():
        root = ROOT / root
    out_dir = ROOT / "tests" / "_bench_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = iter_files(root)
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    if limit > 0:
        files = files[:limit]
    print(f"corpus={root} files={len(files)}")

    settings = _settings()
    rows: list[Row] = []
    for i, path in enumerate(files, 1):
        data = path.read_bytes()
        row = Row(
            path=str(path.relative_to(root)),
            name=path.name,
            ext=path.suffix.lower(),
            size_bytes=len(data),
            engine="?",
            ok=False,
            ms=0.0,
        )
        if not can_parse(path.name):
            row.error = "ENGINE_MISSING"
            rows.append(row)
            continue
        t0 = time.perf_counter()
        try:
            out = parse_document_bytes(data, filename=path.name)
            md = cleanup_markdown(out.markdown)
            row.ms = (time.perf_counter() - t0) * 1000
            row.md_chars = len(md)
            row.engine = out.engine
            row.table_count = out.metrics.table_count
            row.ok = True
            out.metrics.text_length = len(md)
            gate = evaluate_quality(out.metrics, settings)
            row.gate_pass = not gate.triggered
            row.gate_reasons = gate.reasons
            row.density = len(md) / len(data) if data else 0.0
            row.preview = md[:120].replace("\n", " ")
        except Exception as exc:  # noqa: BLE001
            row.ms = (time.perf_counter() - t0) * 1000
            row.error = f"{type(exc).__name__}: {exc}"[:500]
        rows.append(row)
        status = "OK" if row.ok else "FAIL"
        gate = "GATE_OK" if row.gate_pass else ("GATE_SOFT" if row.ok else "-")
        print(
            f"[{i}/{len(files)}] {status} {gate} {row.ms:7.1f}ms "
            f"chars={row.md_chars:6} {path.name[:55]}"
        )
        if row.error:
            print(f"  err={row.error[:160]}")

    ok = [r for r in rows if r.ok]
    gate_ok = [r for r in ok if r.gate_pass]
    times = [r.ms for r in ok]
    summary = {
        "engine": "rhwp",
        "total": len(rows),
        "ok": len(ok),
        "fail": len(rows) - len(ok),
        "gate_pass": len(gate_ok),
        "gate_soft_fail": len(ok) - len(gate_ok),
        "ms_avg": round(sum(times) / len(times), 1) if times else None,
        "ms_sum": round(sum(times), 1) if times else None,
        "qg_density_threshold": settings.qg_density_threshold,
        "qg_mode": settings.qg_mode,
    }
    csv_path = out_dir / "bench_rows.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["gate_reasons"] = "|".join(r.gate_reasons)
            w.writerow(d)
    (out_dir / "bench_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
