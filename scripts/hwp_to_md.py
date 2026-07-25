#!/usr/bin/env python3
"""로컬: HWP/HWPX → Markdown (rhwp-python).

Usage:
  python scripts/hwp_to_md.py input.hwpx
  python scripts/hwp_to_md.py input.hwp -o out.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.parser.cleanup import cleanup_markdown
from services.parser.engine import can_parse, parse_document_bytes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert HWP/HWPX to Markdown (.hwpx=python-hwpx, .hwp=rhwp)"
    )
    parser.add_argument("input", type=Path, help="Path to .hwp or .hwpx")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output .md path")
    args = parser.parse_args()

    src: Path = args.input
    if not src.exists():
        print(f"not found: {src}", file=sys.stderr)
        return 1

    if not can_parse(src.name):
        print(
            "parser engine missing. pip install -r requirements-parser.txt",
            file=sys.stderr,
        )
        return 2

    data = src.read_bytes()
    out = args.output or src.with_suffix(".md")
    result = parse_document_bytes(data, filename=src.name)
    md = cleanup_markdown(result.markdown)
    out.write_text(md, encoding="utf-8")
    print(f"engine={result.engine} wrote {out} ({len(md)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
