"""공유 드라이브 하위 폴더 allowlist 판별 (GCP 클라이언트 의존 없음)."""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def is_under_folder_allowlist(
    *,
    file_id: str,
    parents: list[str],
    allowlist: set[str],
    resolve_parents: Callable[[str], list[str]],
) -> bool:
    """file_id 또는 조상 폴더가 allowlist에 있으면 True.

    allowlist가 비어 있으면 범위 제한 없음(True).
    """
    if not allowlist:
        return True
    if file_id in allowlist:
        return True

    queue = list(parents)
    seen: set[str] = set()
    while queue:
        pid = queue.pop()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        if pid in allowlist:
            return True
        try:
            queue.extend(resolve_parents(pid))
        except Exception as exc:  # noqa: BLE001
            logger.debug("parent resolve failed id=%s: %s", pid, exc)
    return False
