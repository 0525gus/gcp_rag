"""Public MCP search contract. Counts describe returned evidence, not answerability."""

from typing import Any

from typing_extensions import TypedDict


class EvidenceChunk(TypedDict):
    text: str
    score: float
    scoreType: str


class EvidenceSource(TypedDict):
    fileId: str
    name: str
    label: str
    path: str | None
    bundle: str | None
    sourceUri: str | None
    modifiedTime: str | None
    driveId: str | None


class EvidenceDocument(TypedDict):
    citationId: int
    source: EvidenceSource
    chunks: list[EvidenceChunk]


class SearchResponse(TypedDict):
    schemaVersion: int
    documents: list[EvidenceDocument]
    documentCount: int
    chunkCount: int


def build_search_response(documents: list[EvidenceDocument]) -> SearchResponse:
    return {
        "schemaVersion": 2,
        "documents": documents,
        "documentCount": len(documents),
        "chunkCount": sum(len(doc["chunks"]) for doc in documents),
    }


def response_documents(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Read both deployed v1 and v2 MCP results for benchmark/evaluation clients."""
    if result.get("isError"):
        raise ValueError("MCP search returned a tool error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "documents" in structured:
        return structured["documents"]
    import json

    documents = []
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        value = json.loads(block["text"])
        if isinstance(value, dict) and "documents" in value:
            return value["documents"]
        if isinstance(value, list):
            documents.extend(value)
        elif isinstance(value, dict) and "source" in value:
            documents.append(value)
    return documents
