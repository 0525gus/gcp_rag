"""GCS 헬퍼."""

from __future__ import annotations

from google.cloud import storage

from shared.config import Settings, get_settings
from shared.hashing import sha256_bytes, sha256_text

__all__ = [
    "GcsClient",
    "gs_uri",
    "parse_gs_uri",
    "sha256_bytes",
    "sha256_text",
]

# 객체 키는 fileId 하나로 끝난다. 버킷이 이미 분류를 말하므로 prefix 를 두면
# gs://rag-source-x/source/... 처럼 같은 말이 두 번 들어간다.


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")
    rest = uri[5:]
    bucket, _, blob = rest.partition("/")
    if not bucket or not blob:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return bucket, blob


def gs_uri(bucket: str, blob: str) -> str:
    return f"gs://{bucket}/{blob.lstrip('/')}"


class GcsClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = storage.Client(project=self.settings.gcp_project_id)

    def download_bytes(self, uri: str) -> bytes:
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = self._client.bucket(bucket_name).blob(blob_name)
        return blob.download_as_bytes()

    def upload_bytes(
        self,
        data: bytes,
        bucket: str,
        blob_name: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        blob = self._client.bucket(bucket).blob(blob_name)
        blob.upload_from_string(data, content_type=content_type)
        return gs_uri(bucket, blob_name)

    def upload_text(self, text: str, bucket: str, blob_name: str) -> str:
        return self.upload_bytes(
            text.encode("utf-8"),
            bucket,
            blob_name,
            content_type="text/markdown; charset=utf-8",
        )

    def upload_hwp_original(self, data: bytes, file_id: str, ext: str) -> str:
        """HWP/HWPX 원본 업로드. 파서가 이 URI 를 받아 MD 로 변환한다."""
        blob = f"{file_id}{ext}"
        return self.upload_bytes(data, self.settings.gcs_hwp_original_bucket, blob)

    def upload_source_md(self, markdown: str, file_id: str) -> str:
        blob = f"{file_id}.md"
        return self.upload_text(markdown, self.settings.gcs_source_bucket, blob)

    def upload_source_sidecar_md(self, markdown: str, file_id: str) -> str:
        """바이너리(PDF/PPTX 등)용 경로·묶음 메타 MD (본문과 함께 RAG import)."""
        blob = f"{file_id}.meta.md"
        return self.upload_text(markdown, self.settings.gcs_source_bucket, blob)

    def delete(self, uri: str) -> None:
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = self._client.bucket(bucket_name).blob(blob_name)
        if blob.exists():
            blob.delete()

    def list_blob_names(self, bucket: str, prefix: str) -> list[str]:
        """prefix 아래 객체 이름 전체 (정리·감사용 전수 조회)."""
        return [
            blob.name
            for blob in self._client.list_blobs(self._client.bucket(bucket), prefix=prefix)
        ]

    def list_blob_names_for_file(self, bucket: str, file_id: str) -> list[str]:
        """해당 fileId 의 객체 이름만 반환.

        확장자를 미리 알 수 없어서 fileId 로 훑는다. 확장자 목록을 손으로 적는
        방식은 목록에 없는 것을 조용히 놓친다 — 실측으로 `.partN.pdf`(분할 PDF)와
        `.rtf`/`.doc` 가 빠져 있었다.

        `rest` 검사가 fileId 경계를 지킨다. 접두 일치만으로 걸면 fileId 가 다른
        fileId 의 접두사일 때 남의 파일을 지운다.
        """
        names: list[str] = []
        for blob in self._client.list_blobs(self._client.bucket(bucket), prefix=file_id):
            rest = blob.name[len(file_id) :]
            if rest and not rest.startswith("."):
                continue
            names.append(blob.name)
        return names

    def delete_for_file(self, bucket: str, file_id: str) -> list[str]:
        """해당 fileId 의 객체를 전부 지우고 지운 이름을 반환."""
        deleted: list[str] = []
        for name in self.list_blob_names_for_file(bucket, file_id):
            self._client.bucket(bucket).blob(name).delete()
            deleted.append(name)
        return deleted
