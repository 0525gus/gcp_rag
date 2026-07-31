"""`SKIPPED` 중 대상 폴더 밖인 것을 `EXCLUDED` 로 옮긴다 (일회성).

배경 — 왜 나누는가

`SKIPPED` 하나에 성격이 다른 둘이 뭉쳐 있었다.

    대상인데 처리 못 함   미지원 MIME, 암호 걸린 xlsx/PDF …
    애초에 대상이 아님     동기화 지정 폴더 밖

실측(2026-07-30) 393건이 전부 후자였고, 그것이 `reconcile` 의 `accounted` 에
들어가 정합성 계산에 섞였다. 그래서 "대상인데 처리 못 한 것"이 몇 건인지
알 수 없었다 — 신호가 잡음에 묻힌 것이다.

앞으로 들어오는 문서는 `ingest` 가 알아서 갈라 준다. 이 스크립트는 **이미
쌓인 것**만 한 번 옮긴다.

판별 근거는 `error == "out_of_folder_scope"` 다. 이 값은 범위 밖 경로에서만
기록되므로(`services/sync/main.py` 의 `_mark_excluded`) 다른 사유의 SKIPPED 를
잘못 건드릴 수 없다.

사용:
  python scripts/migrate_excluded_status.py            # 조회만 (기본)
  python scripts/migrate_excluded_status.py --apply    # 실제 갱신

인증: gcloud application-default 자격증명이 필요하다
      (`gcloud auth application-default login`).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout, load_dotenv
from shared.config import get_settings
from shared.logging_config import setup_logging
from shared.models import DocStatus

logger = logging.getLogger("migrate_excluded")

OUT_OF_SCOPE = "out_of_folder_scope"


def main() -> int:
    force_utf8_stdout()
    load_dotenv()
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument(
        "--apply", action="store_true", help="실제로 갱신 (기본은 조회만)"
    )
    args = ap.parse_args()

    from shared.firestore_state import DocStateStore

    settings = get_settings()
    store = DocStateStore(settings)
    col = store._db.collection(settings.firestore_collection)

    targets: list[str] = []
    other_reasons: Counter[str] = Counter()
    for snap in col.where("status", "==", DocStatus.SKIPPED.value).stream():
        data = snap.to_dict() or {}
        if (data.get("error") or "") == OUT_OF_SCOPE:
            targets.append(snap.id)
        else:
            other_reasons[(data.get("error") or "(사유 없음)")[:60]] += 1

    print(f"SKIPPED → EXCLUDED 대상: {len(targets)}건")
    if other_reasons:
        print(f"SKIPPED 로 남는 것: {sum(other_reasons.values())}건")
        for reason, n in other_reasons.most_common(10):
            print(f"  {reason:<50} {n}건")

    if not args.apply:
        print("  (조회만: 갱신은 --apply)")
        return 0
    if not targets:
        return 0

    # Firestore 배치는 1회당 500건 — 나눠 커밋한다
    done = 0
    for start in range(0, len(targets), 400):
        batch = store._db.batch()
        for file_id in targets[start : start + 400]:
            batch.set(
                col.document(file_id),
                {"status": DocStatus.EXCLUDED.value},
                merge=True,
            )
        batch.commit()
        done += len(targets[start : start + 400])
        logger.info("migrated %s/%s", done, len(targets))

    print(f"갱신 완료 {done}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
