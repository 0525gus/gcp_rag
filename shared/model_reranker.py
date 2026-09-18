"""Optional managed cross-encoder and Gemini rerankers with fail-open ordering."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import google.auth
from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(frozen=True)
class RerankRecord:
    id: str
    title: str
    path: str
    bundle: str
    metadata: str
    chunk: str

    def content(self, limit: int = 6000) -> str:
        value = (
            f"path: {self.path}\nbundle: {self.bundle}\nmetadata: {self.metadata}\n"
            f"chunk: {self.chunk}"
        )
        return value[:limit]


def _validated_order(ids: list[str], records: list[RerankRecord]) -> list[int] | None:
    index = {record.id: i for i, record in enumerate(records)}
    if not ids or any(value not in index for value in ids) or len(set(ids)) != len(ids):
        return None
    ordered = [index[value] for value in ids]
    # Models sometimes return only the confident head. Preserve its ranking and
    # append omitted candidates in base-RRF order instead of discarding the call.
    selected = set(ordered)
    return ordered + [i for i in range(len(records)) if i not in selected]


def cross_encoder_order(
    query: str,
    records: list[RerankRecord],
    *,
    project_id: str,
    model: str = "semantic-ranker-default-004",
    timeout: float = 10.0,
    session: AuthorizedSession | None = None,
) -> list[int] | None:
    if not records:
        return []
    try:
        if session is None:
            credentials, _ = google.auth.default(scopes=[_SCOPE])
            session = AuthorizedSession(credentials)
        config = f"projects/{project_id}/locations/global/rankingConfigs/default_ranking_config"
        response = session.post(
            f"https://discoveryengine.googleapis.com/v1/{config}:rank",
            json={
                "model": model,
                "topN": len(records),
                "query": query,
                "records": [
                    {"id": record.id, "title": record.title, "content": record.content()}
                    for record in records
                ],
                "ignoreRecordDetailsInResponse": True,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return _validated_order(
            [str(item.get("id", "")) for item in response.json().get("records", [])],
            records,
        )
    except Exception:
        logger.warning("cross-encoder reranker unavailable; retaining RRF order", exc_info=True)
        return None


_LLM_PROMPT = """Rank all candidate records by how well they answer the query.
Use title, path, bundle, metadata, and chunk. Preserve date, sequence, document-kind,
and revision constraints. Return every candidate id exactly once, best first."""


def llm_reranker_order(
    query: str,
    records: list[RerankRecord],
    *,
    project_id: str,
    model: str = "gemini-2.5-flash-lite",
    timeout: float = 15.0,
    session: AuthorizedSession | None = None,
    metrics: dict[str, int] | None = None,
) -> list[int] | None:
    if not records:
        return []
    try:
        if session is None:
            credentials, _ = google.auth.default(scopes=[_SCOPE])
            session = AuthorizedSession(credentials)
        url = (
            f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/"
            f"publishers/google/models/{model}:generateContent"
        )
        candidates = [
            {
                "id": record.id,
                "title": record.title,
                "path": record.path,
                "bundle": record.bundle,
                "metadata": record.metadata,
                "chunk": record.chunk[:3000],
            }
            for record in records
        ]
        response = session.post(
            url,
            json={
                "systemInstruction": {"parts": [{"text": _LLM_PROMPT}]},
                "contents": [{
                    "role": "user",
                    "parts": [{"text": json.dumps(
                        {"query": query, "candidates": candidates}, ensure_ascii=False
                    )}],
                }],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 1024,
                    "thinkingConfig": {"thinkingBudget": 0},
                    "responseMimeType": "application/json",
                    "responseSchema": {
                        "type": "OBJECT",
                        "properties": {
                            "ordered_ids": {"type": "ARRAY", "items": {"type": "STRING"}}
                        },
                        "required": ["ordered_ids"],
                    },
                },
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        usage = body.get("usageMetadata", {})
        if metrics is not None:
            metrics.update({
                "input_tokens": int(usage.get("promptTokenCount", 0)),
                "output_tokens": int(usage.get("candidatesTokenCount", 0)),
                "total_tokens": int(usage.get("totalTokenCount", 0)),
            })
        parts = body["candidates"][0]["content"]["parts"]
        payload = json.loads("".join(str(part.get("text", "")) for part in parts))
        return _validated_order([str(value) for value in payload["ordered_ids"]], records)
    except Exception:
        logger.warning("LLM reranker unavailable; retaining RRF order", exc_info=True)
        return None
