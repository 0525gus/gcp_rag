from __future__ import annotations

import argparse
import csv
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout  # noqa: E402
from scripts.dept_config import load_config_env  # noqa: E402

LIVE_STATUSES = ("INDEXED", "PARSED")


def drive_state(get_file: Callable[[str], dict[str, Any]], file_id: str, drive_id: str) -> str:
    from googleapiclient.errors import HttpError

    try:
        meta = get_file(file_id)
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status == 404:
            return "missing"
        if status == 403:
            return "no-access"
        raise
    if meta.get("trashed"):
        return "trashed"
    if meta.get("driveId") and drive_id and meta["driveId"] != drive_id:
        return "other-drive"
    return "ok"


def find_orphans(
    docs: Iterable[dict[str, Any]],
    get_file: Callable[[str], dict[str, Any]],
    *,
    workers: int = 8,
) -> list[dict[str, Any]]:
    live = [d for d in docs if d.get("status") in LIVE_STATUSES]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        states = list(pool.map(lambda d: drive_state(get_file, d["fileId"], d.get("driveId") or ""), live))
    return [{**d, "driveState": s} for d, s in zip(live, states) if s != "ok"]


def _thread_local_get_file() -> Callable[[str], dict[str, Any]]:
    from shared.drive import DriveClient

    local = threading.local()

    def get_file(file_id: str) -> dict[str, Any]:
        if not hasattr(local, "drive"):
            local.drive = DriveClient()
        return local.drive.get_file(file_id)

    return get_file


def _doc_states(settings) -> list[dict[str, Any]]:
    from google.cloud import firestore

    db = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_database)
    rows = []
    for snap in db.collection(settings.doc_state_collection).stream():
        data = snap.to_dict() or {}
        rows.append({
            "fileId": snap.id,
            "driveId": data.get("driveId"),
            "audience": data.get("audience"),
            "status": data.get("status"),
            "path": data.get("path"),
        })
    return rows


def main() -> int:
    force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="색인된 문서(INDEXED·PARSED) 중 Drive 에서 사라졌거나 휴지통에 있는 문서를 찾는다. 조회만 한다."
    )
    parser.add_argument("--csv", help="결과를 CSV 로 저장")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    load_config_env()
    from shared.config import get_settings

    orphans = find_orphans(_doc_states(get_settings()), _thread_local_get_file(), workers=args.workers)
    for state, count in Counter((o["audience"], o["driveState"]) for o in orphans).most_common():
        print(f"{state[0]} {state[1]}: {count}건")
    for row in orphans:
        print(f"  {row['driveState']:<11} {row['status']:<8} {row['fileId']}  {row['path']}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["fileId", "driveId", "audience", "status", "driveState", "path"]
            )
            writer.writeheader()
            writer.writerows(orphans)
    if not orphans:
        print("Drive 와 어긋난 색인 문서 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
