"""Parser for Vertex RAG ``import_result_sink`` NDJSON output."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_RAG_FILE_RE = re.compile(
    r"^projects/[^/]+/locations/[^/]+/ragCorpora/[^/]+/ragFiles/[^/]+$"
)


@dataclass(frozen=True)
class RagImportResult:
    gcs_uri: str
    rag_file_name: str
    status: str
    error: str | None = None
    import_result_sink: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status.upper() in {"OK", "SUCCESS", "SUCCEEDED", "ACTIVE"}


def _walk(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (str, int)) and not isinstance(child, bool):
                yield str(key), str(child)
            else:
                yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(data: dict[str, Any], keys: set[str], predicate=None) -> str:
    for key, value in _walk(data):
        normalized = key.replace("_", "").lower()
        if normalized in keys and (predicate is None or predicate(value)):
            return value
    if predicate:
        for _key, value in _walk(data):
            if predicate(value):
                return value
    return ""


def parse_import_results(
    payload: bytes | str, *, corpus_name: str = "", sink_uri: str | None = None
) -> list[RagImportResult]:
    """Parse successful and failed rows without depending on field casing.

    Vertex has exposed this sink through both v1beta1 and v1 SDKs.  Keeping the
    aliases narrow but casing-agnostic makes rolling SDK upgrades survivable;
    rows without both a source URI and resource name remain observable as a
    failure instead of being guessed into a mapping.
    """
    text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
    results: list[RagImportResult] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError(f"import result line {line_no} is not an object")
        gcs_uri = _first(
            data,
            {"filename", "gcsuri", "sourceuri", "uri"},
            lambda value: value.startswith("gs://"),
        )
        rag_file_name = _first(
            data,
            {"ragfilename", "ragfile", "name", "ragfileid"},
            lambda value: bool(_RAG_FILE_RE.match(value)),
        )
        if not rag_file_name:
            rag_file_id = _first(
                data,
                {"fileid", "ragfileid"},
                lambda value: value.isdigit(),
            )
            if rag_file_id and corpus_name:
                rag_file_name = f"{corpus_name.rstrip('/')}/ragFiles/{rag_file_id}"
        status = _first(data, {"status", "state", "result"}).upper() or (
            "SUCCEEDED" if rag_file_name else "FAILED"
        )
        error = _first(data, {"error", "errormessage", "message"}) or None
        results.append(
            RagImportResult(
                gcs_uri=gcs_uri,
                rag_file_name=rag_file_name,
                status=status,
                error=error,
                import_result_sink=sink_uri,
            )
        )
    return results
