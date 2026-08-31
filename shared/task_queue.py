"""Cloud Tasks HTTP task producer for split RAG indexing."""

from __future__ import annotations

import json
from typing import Any

from google.api_core import exceptions as gcp_exceptions
from google.cloud import tasks_v2

from shared.config import Settings


class IndexTaskQueue:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = tasks_v2.CloudTasksClient()

    def enqueue(
        self,
        *,
        queue: str,
        task_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Create one deterministic, OIDC-authenticated HTTP task.

        A repeated workflow request can attempt to create the same task. Cloud
        Tasks retains task names after completion, so ALREADY_EXISTS is treated
        as idempotent success.
        """
        base_url = self.settings.sync_task_base_url.rstrip("/")
        if not base_url or not self.settings.task_service_account:
            raise RuntimeError(
                "SYNC_TASK_BASE_URL and TASK_SERVICE_ACCOUNT are required"
            )
        parent = self._client.queue_path(
            self.settings.gcp_project_id,
            self.settings.task_queue_location,
            queue,
        )
        name = self._client.task_path(
            self.settings.gcp_project_id,
            self.settings.task_queue_location,
            queue,
            task_id,
        )
        task = tasks_v2.Task(
            name=name,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{base_url}/sync/index-gcs-task",
                headers={"Content-Type": "application/json"},
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.settings.task_service_account,
                    audience=base_url,
                ),
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            ),
        )
        try:
            created = self._client.create_task(parent=parent, task=task)
            return created.name
        except gcp_exceptions.AlreadyExists:
            return name
