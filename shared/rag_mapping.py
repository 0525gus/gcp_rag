"""Drive fileId -> Vertex RagFile resource-name mappings.

A Drive document can produce more than one GCS object and can exist in both the
staff and student corpora.  The old singular ``doc_state.ragFileId`` therefore
cannot represent the real relationship.  Mappings live in a subcollection so
the two corpus workers can update their own rows without overwriting each other.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore

from shared.config import Settings, get_settings


@dataclass(frozen=True)
class RagFileMapping:
    file_id: str
    corpus_type: str
    corpus_name: str
    rag_file_name: str
    gcs_uri: str
    generation: str
    status: str = "ACTIVE"
    import_result_sink: str | None = None
    updated_at: datetime | None = None

    @property
    def mapping_id(self) -> str:
        # Backfill 대상인 기존 RagFile은 SDK 버전에 따라 source_uri가 비어 있을
        # 수 있다. 이때도 RagFile resource name은 항상 고유하므로 안정적인 키로
        # 쓸 수 있다. 이후 정상 import가 들어오면 replace_for_corpus가 이 임시
        # backfill 행을 새 gcs_uri 기반 행으로 교체한다.
        identity = self.gcs_uri or self.rag_file_name
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return f"{self.corpus_type.lower()}__{digest}"

    def to_firestore(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "corpusType": self.corpus_type.upper(),
            "corpusName": self.corpus_name,
            "ragFileName": self.rag_file_name,
            "gcsUri": self.gcs_uri,
            "generation": self.generation,
            "status": self.status,
            "importResultSink": self.import_result_sink,
            "updatedAt": self.updated_at or datetime.now(UTC),
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> RagFileMapping:
        return cls(
            file_id=str(data.get("fileId") or ""),
            corpus_type=str(data.get("corpusType") or "").upper(),
            corpus_name=str(data.get("corpusName") or ""),
            rag_file_name=str(data.get("ragFileName") or ""),
            gcs_uri=str(data.get("gcsUri") or ""),
            generation=str(data.get("generation") or ""),
            status=str(data.get("status") or "ACTIVE"),
            import_result_sink=data.get("importResultSink"),
            updated_at=data.get("updatedAt"),
        )


class RagFileMappingStore:
    """Firestore-backed 1:N mapping store.

    ``replace_for_corpus`` is deliberately a batch: readers must never observe
    a half-replaced generation after an import completes.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = firestore.Client(
            project=self.settings.gcp_project_id,
            database=self.settings.firestore_database,
        )
        self._docs = self._db.collection(self.settings.doc_state_collection)

    def _mappings(self, file_id: str):
        return self._docs.document(file_id).collection("rag_files")

    def list_for_file(
        self, file_id: str, *, corpus_type: str | None = None
    ) -> list[RagFileMapping]:
        rows = []
        wanted = corpus_type.upper() if corpus_type else None
        for snap in self._mappings(file_id).stream():
            data = snap.to_dict() or {}
            data.setdefault("fileId", file_id)
            mapping = RagFileMapping.from_firestore(data)
            if wanted is None or mapping.corpus_type == wanted:
                rows.append(mapping)
        return rows

    def replace_for_corpus(
        self,
        file_id: str,
        corpus_type: str,
        mappings: Iterable[RagFileMapping],
    ) -> None:
        wanted = corpus_type.upper()
        incoming = list(mappings)
        if any(m.file_id != file_id or m.corpus_type.upper() != wanted for m in incoming):
            raise ValueError("mapping file_id/corpus_type mismatch")

        existing = self.list_for_file(file_id, corpus_type=wanted)
        batch = self._db.batch()
        collection = self._mappings(file_id)
        incoming_ids = {m.mapping_id for m in incoming}
        for old in existing:
            if old.mapping_id not in incoming_ids:
                batch.delete(collection.document(old.mapping_id))
        for mapping in incoming:
            batch.set(
                collection.document(mapping.mapping_id),
                mapping.to_firestore(),
                merge=True,
            )
        batch.commit()

    def upsert_many(
        self,
        mappings: Iterable[RagFileMapping],
        *,
        batch_size: int = 400,
    ) -> int:
        """기존 매핑을 지우지 않고 여러 행을 일괄 보강한다.

        초기 backfill 전용이다. Firestore batch 한도(500)보다 여유 있게 끊고,
        기존 dual-write 행은 merge로 보존한다. 정확한 세대 교체는 이후 파일별
        import의 ``replace_for_corpus``가 담당한다.
        """
        if batch_size < 1 or batch_size > 500:
            raise ValueError("batch_size must be between 1 and 500")

        rows = list(mappings)
        for start in range(0, len(rows), batch_size):
            batch = self._db.batch()
            for mapping in rows[start : start + batch_size]:
                batch.set(
                    self._mappings(mapping.file_id).document(mapping.mapping_id),
                    mapping.to_firestore(),
                    merge=True,
                )
            batch.commit()
        return len(rows)

    def delete(self, mapping: RagFileMapping) -> None:
        self._mappings(mapping.file_id).document(mapping.mapping_id).delete()
