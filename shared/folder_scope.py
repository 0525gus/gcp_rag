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
    if not allowlist:
        return True
    if file_id in allowlist:
        return True

    queue = list(parents)
    seen: set[str] = set()
    resolution_error: Exception | None = None
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
            resolution_error = exc
            logger.warning("parent resolve failed id=%s: %s", pid, exc)
    if resolution_error is not None:
        raise RuntimeError(
            f"cannot determine folder scope for {file_id}: parent lookup failed"
        ) from resolution_error
    return False
