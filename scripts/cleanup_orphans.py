from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout  # noqa: E402
from scripts.dept_config import load_config_env  # noqa: E402
from shared.config import Settings, get_settings  # noqa: E402
from shared.gcs import GcsClient, gs_uri  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.models import DocStatus  # noqa: E402
from shared.search_postprocess import extract_file_id  # noqa: E402

logger = logging.getLogger("cleanup_orphans")

_LIVE_STATUSES = frozenset(
    {
        DocStatus.INDEXED.value,
        DocStatus.PARSED.value,
        DocStatus.PENDING.value,
        DocStatus.SKIPPED.value,
        DocStatus.FAILED.value,
    }
)

REASON_DELETED = "doc_state=DELETED"
REASON_EXCLUDED = "doc_state=EXCLUDED (대상 폴더 밖)"
REASON_UNKNOWN = "doc_state 에 없음"


@dataclass(frozen=True)
class Candidate:
    uri: str
    file_id: str
    doc_status: str
    reason: str


def classify(
    blob_name: str, doc_status: dict[str, str], *, only_deleted: bool = False
) -> tuple[str, str] | None:
    file_id = extract_file_id(blob_name)
    if not file_id or file_id == "unknown":
        return None
    status = doc_status.get(file_id)
    if status in _LIVE_STATUSES:
        return None
    if status == DocStatus.DELETED.value:
        return file_id, REASON_DELETED
    if status == DocStatus.EXCLUDED.value:
        return None if only_deleted else (file_id, REASON_EXCLUDED)
    if status is None:
        return None if only_deleted else (file_id, REASON_UNKNOWN)
    return None


def _load_doc_status(settings: Settings) -> dict[str, str]:
    from shared.firestore_state import DocStateStore

    return DocStateStore(settings).all_statuses()


def dept_settings(base: Settings) -> list[tuple[str, Settings]]:
    if not base.departments:
        return [("", base)]
    return [(d.code, base.for_drive(d.drive_ids[0])) for d in base.departments if d.drive_ids]


def collect(
    settings: Settings, doc_status: dict[str, str], *, only_deleted: bool
) -> list[Candidate]:
    gcs = GcsClient(settings)
    targets = [settings.gcs_hwp_original_bucket, settings.gcs_source_bucket]
    found: list[Candidate] = []
    for bucket in targets:
        if not bucket:
            continue
        for name in gcs.list_blob_names(bucket, ""):
            verdict = classify(name, doc_status, only_deleted=only_deleted)
            if verdict is None:
                continue
            file_id, reason = verdict
            found.append(
                Candidate(
                    uri=gs_uri(bucket, name),
                    file_id=file_id,
                    doc_status=doc_status.get(file_id) or "(none)",
                    reason=reason,
                )
            )
    return sorted(found, key=lambda c: c.uri)


def _report(candidates: list[Candidate], total_note: str) -> None:
    print(f"삭제 대상: {len(candidates)}건{total_note}")
    for key, label in (("bucket", "버킷"), ("reason", "이유")):
        counts = Counter(
            (c.uri.split("/")[2] if key == "bucket" else c.reason) for c in candidates
        )
        for name, n in counts.most_common():
            print(f"  {label} {name:<34} {n}건")
    ext = Counter(os.path.splitext(c.uri)[1] or "(없음)" for c in candidates)
    for name, n in ext.most_common(8):
        print(f"  확장자 {name:<32} {n}건")


def main() -> int:
    force_utf8_stdout()
    setup_logging()
    ap = argparse.ArgumentParser(description='GCS 에 남은 잔존 객체를 doc_state 와 대조해 정리한다.')
    ap.add_argument(
        "--apply", action="store_true", help="실제로 삭제 (기본은 조회만)"
    )
    ap.add_argument(
        "--only-deleted",
        action="store_true",
        help="doc_state=DELETED 만 대상. 상태를 모르는 객체는 건드리지 않는다",
    )
    ap.add_argument("--csv", help="근거 목록을 CSV 로 저장")
    ap.add_argument("--dept", help="이 학과만 (기본: 전 학과)")
    args = ap.parse_args()

    load_config_env()

    settings = get_settings()
    doc_status = _load_doc_status(settings)
    live = sum(1 for s in doc_status.values() if s in _LIVE_STATUSES)
    print(f"doc_state {len(doc_status)}건 (살아 있는 문서 {live}건)")

    targets = dept_settings(settings)
    if args.dept:
        targets = [(code, s) for code, s in targets if code == args.dept]
        if not targets:
            raise SystemExit(f"학과 맵에 없다: {args.dept}")
    candidates: list[Candidate] = []
    for code, dept in targets:
        found = collect(dept, doc_status, only_deleted=args.only_deleted)
        if code:
            print(f"  학과 {code:<10} {len(found)}건")
        candidates.extend(found)
    candidates.sort(key=lambda c: c.uri)
    _report(candidates, "" if args.apply else "  (조회만: 삭제는 --apply)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(("uri", "fileId", "docStatus", "reason"))
            for c in candidates:
                writer.writerow((c.uri, c.file_id, c.doc_status, c.reason))
        print(f"근거 저장: {args.csv}")

    if not args.apply or not candidates:
        return 0

    gcs = GcsClient(settings)
    failed = 0
    for c in candidates:
        try:
            gcs.delete(c.uri)
            logger.info("deleted %s (%s)", c.uri, c.reason)
        except Exception:
            failed += 1
            logger.exception("삭제 실패 %s", c.uri)
    print(f"삭제 완료 {len(candidates) - failed}건 / 실패 {failed}건")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
