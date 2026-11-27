from __future__ import annotations

import threading
import time
from typing import Any


class RunStore:
    def __init__(self, ttl_seconds: float, items: str = "steps") -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.private: dict[str, Any] = {}
        self.ttl_seconds = ttl_seconds
        self.items = items

    def cleanup(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for plan_id in [key for key, plan in self.plans.items() if plan.get("createdEpoch", 0) < cutoff]:
            del self.plans[plan_id]
        for run_id in [
            key
            for key, run in self.runs.items()
            if run.get("finishedEpoch", run.get("createdEpoch", time.time())) < cutoff
        ]:
            del self.runs[run_id]
            self.private.pop(run_id, None)

    def update_item(self, run_id: str, key: str, **changes: Any) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return
            for item in run[self.items]:
                if item["key"] == key:
                    item.update(changes)
                    return

    def fail_running(self, run_id: str, message: str) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return
            running = next((item for item in run[self.items] if item["status"] == "RUNNING"), None)
            if running:
                running.update(status="FAILED", detail=message)
            run.update(status="FAILED", error=message, finishedEpoch=time.time())
            self.private.pop(run_id, None)

    def clear(self) -> None:
        with self.lock:
            self.runs.clear()
            self.plans.clear()
            self.private.clear()
