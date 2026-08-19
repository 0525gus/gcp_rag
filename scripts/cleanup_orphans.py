"""GCS 에 남은 잔존 객체를 doc_state 와 대조해 정리한다.

배경 — 왜 이 스크립트가 필요한가

`/sync/delete` 는 한동안 source 버킷 만, 그것도 손으로 적은 확장자 목록으로
지웠다. hwp-original 버킷 는 아예 손대지 않았다. 그래서 Drive 에서 지운 문서의 **원본이
GCS 에 영구 잔존**했다(2026-07-30 실측: DELETED 100건 중 52건의 `.hwp` 원본).
hwp-original 버킷 에는 명단·인사발령 같은 원문이 그대로 있어(docs/OPS_DEFERRED.md 6번)
삭제가 이행되지 않는 것 자체가 문제다.

삭제 경로는 prefix 훑기로 고쳤으므로 앞으로 새 잔존물은 생기지 않는다. 이
스크립트는 (1) 고치기 전에 쌓인 것을 한 번 걷어내고, (2) 일시 오류로 삭제가
반쯤 끝난 경우를 나중에 다시 훑기 위한 것이다.

지우는 것이 안전한 이유: 두 버킷 모두 **파생물**이다. hwp-original 버킷 는 ingest 마다
Drive 에서 다시 받아 올리고(`_ingest_hwp`: download_file → upload_hwp_original), 그
직후 파서가 한 번 읽는 것 외에는 아무도 읽지 않는다. source 버킷도 재색인
경로가 다시 만든다. 원본은 Drive 다.

사용:
  python scripts/cleanup_orphans.py                 # 조회만 (기본)
  python scripts/cleanup_orphans.py --csv out.csv   # 근거를 파일로
  python scripts/cleanup_orphans.py --apply         # 실제 삭제
  python scripts/cleanup_orphans.py --apply --only-deleted   # DELETED 만

인증: gcloud application-default 자격증명이 필요하다
      (`gcloud auth application-default login`).
"""

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

from scripts._env import force_utf8_stdout, load_dotenv  # noqa: E402
from shared.config import Settings, get_settings  # noqa: E402
from shared.gcs import GcsClient, gs_uri  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.models import DocStatus  # noqa: E402
from shared.search_postprocess import extract_file_id  # noqa: E402

logger = logging.getLogger("cleanup_orphans")

# 문서가 살아 있는 상태 — 이 상태의 객체는 절대 건드리지 않는다
_LIVE_STATUSES = frozenset(
    {
        DocStatus.INDEXED.value,
        DocStatus.PARSED.value,
        DocStatus.PENDING.value,
        DocStatus.SKIPPED.value,
        # FAILED 는 /sync/retry-failed 가 다시 집어가므로 산출물을 남겨 둔다
        DocStatus.FAILED.value,
        # EXCLUDED 는 여기 없다 — 대상 폴더 밖으로 나간 문서의 GCS·코퍼스
        # 잔존물은 회수해야 한다. SKIPPED 에 뭉쳐 있던 동안에는 '살아있는
        # 문서'로 취급돼 아무도 정리하지 않았다.
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
    """(fileId, 삭제 이유). 지울 이유가 없으면 None.

    GCP 의존이 없는 순수 함수 — 판정 규칙은 여기서만 바뀐다.
    """
    file_id = extract_file_id(blob_name)
    if not file_id or file_id == "unknown":
        return None
    status = doc_status.get(file_id)
    if status in _LIVE_STATUSES:
        return None
    if status == DocStatus.DELETED.value:
        return file_id, REASON_DELETED
    if status == DocStatus.EXCLUDED.value:
        # 대상 폴더 밖으로 나간 문서 — 산출물을 남겨 둘 이유가 없다.
        # --only-deleted 로 돌릴 때는 건드리지 않는다(보수적 실행용 스위치).
        return None if only_deleted else (file_id, REASON_EXCLUDED)
    if status is None:
        # 상태를 모르는 객체. doc_state 를 날린 뒤라면 살아 있는 문서일 수도
        # 있으므로 기본적으로는 지우되, --only-deleted 로 뺄 수 있게 둔다.
        return None if only_deleted else (file_id, REASON_UNKNOWN)
    return None


def _load_doc_status(settings: Settings) -> dict[str, str]:
    from shared.firestore_state import DocStateStore

    return DocStateStore(settings).all_statuses()


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
    load_dotenv()
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument(
        "--apply", action="store_true", help="실제로 삭제 (기본은 조회만)"
    )
    ap.add_argument(
        "--only-deleted",
        action="store_true",
        help="doc_state=DELETED 만 대상. 상태를 모르는 객체는 건드리지 않는다",
    )
    ap.add_argument("--csv", help="근거 목록을 CSV 로 저장")
    args = ap.parse_args()

    settings = get_settings()
    doc_status = _load_doc_status(settings)
    live = sum(1 for s in doc_status.values() if s in _LIVE_STATUSES)
    print(f"doc_state {len(doc_status)}건 (살아 있는 문서 {live}건)")

    candidates = collect(settings, doc_status, only_deleted=args.only_deleted)
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
            # 한 건이 실패해도 나머지는 계속 — 마지막에 건수로 보고한다
            failed += 1
            logger.exception("삭제 실패 %s", c.uri)
    print(f"삭제 완료 {len(candidates) - failed}건 / 실패 {failed}건")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
