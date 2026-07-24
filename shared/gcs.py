"""GCS 헬퍼."""

from __future__ import annotations

from pathlib import Path

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

    def download_to_path(self, uri: str, path: str | Path) -> Path:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = self._client.bucket(bucket_name).blob(blob_name)
        blob.download_to_filename(str(dest))
        return dest

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

    def upload_raw(self, data: bytes, file_id: str, ext: str) -> str:
        blob = f"raw/{file_id}{ext}"
        return self.upload_bytes(data, self.settings.gcs_raw_bucket, blob)

    def upload_normalized_md(self, markdown: str, file_id: str) -> str:
        blob = f"normalized/{file_id}.md"
        return self.upload_text(markdown, self.settings.gcs_normalized_bucket, blob)

    def upload_path_sidecar_md(self, markdown: str, file_id: str) -> str:
        """바이너리(PDF/PPTX 등)용 경로·묶음 메타 MD (본문과 함께 RAG import)."""
        blob = f"normalized/{file_id}.meta.md"
        return self.upload_text(markdown, self.settings.gcs_normalized_bucket, blob)

    def delete(self, uri: str) -> None:
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = self._client.bucket(bucket_name).blob(blob_name)
        if blob.exists():
            blob.delete()
