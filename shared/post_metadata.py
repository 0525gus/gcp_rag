"""Validated, deterministic metadata supplied by the source collector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

METADATA_FILENAME = "metadata.json"
METADATA_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class PostMetadata:
    title: str = ""
    source_url: str = ""


def parse_post_metadata(payload: bytes) -> PostMetadata:
    if len(payload) > METADATA_MAX_BYTES:
        raise ValueError("metadata.json exceeds 64 KiB")
    data = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("metadata.json must contain an object")  # noqa: TRY004 - invalid metadata is a value error
    title, url = data.get("title", ""), data.get("source_url", "")
    if not isinstance(title, str) or not isinstance(url, str):
        raise ValueError("title and source_url must be strings")  # noqa: TRY004
    title, url = title.strip(), url.strip()
    if len(title) > 1000 or any(ord(c) < 32 for c in title):
        raise ValueError("invalid metadata title")
    if url:
        parsed = urlsplit(url)
        if (
            len(url) > 4096
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(c.isspace() or ord(c) < 32 for c in url)
        ):
            raise ValueError("source_url must be an HTTP(S) URL without credentials")
        _ = parsed.port  # validates malformed ports
    return PostMetadata(title=title, source_url=url)
