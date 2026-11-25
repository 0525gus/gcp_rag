#!/usr/bin/env python3

from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import services.sync.main as sync_main  # noqa: E402
from services.parser.cleanup import cleanup_markdown  # noqa: E402
from services.parser.engine import parse_document_bytes  # noqa: E402
from services.parser.quality_gate import (  # noqa: E402
    count_markdown_tables,
    evaluate_quality,
)
from shared.config import Settings  # noqa: E402
from shared.hashing import sha256_text  # noqa: E402
from shared.models import DocStatus  # noqa: E402
from shared.path_context import PathContext  # noqa: E402

# Drive 가 실제로 붙이는 MIME. 확장자로 추측하면 라우트가 달라져 검증이 무의미해진다.
EXT_MIME = {
    ".hwp": "application/x-hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".xls": "application/vnd.ms-excel",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".zip": "application/zip",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


class FakeDrive:
    def __init__(self, files: dict[str, Path], root: Path) -> None:
        self.files = files
        self.root = root
        self.downloads: list[str] = []

    def download_file(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self.files[file_id].read_bytes()

    def resolve_path_context(self, file_id: str, fallback: str) -> PathContext:
        rel = self.files[file_id].relative_to(self.root)
        segs = ("Drive", *rel.parts[:-1])
        return PathContext(
            path="/".join(segs),
            bundle=segs[-1] if len(segs) > 1 else "",
            segments=segs,
        )

    def is_in_sync_scope(self, file_id: str, folder_ids: Any) -> bool:
        return True


class FakeGcs:
    def __init__(self) -> None:
        self.raw: list[str] = []
        self.md: list[tuple[str, str]] = []
        self.blobs: list[str] = []
        self.sidecars: list[str] = []
        self.deleted: list[str] = []
        self.objects: dict[str, bytes] = {}

    def upload_hwp_original(self, data: bytes, file_id: str, ext: str) -> str:
        self.raw.append(f"{file_id}{ext}")
        return f"gs://rb/{file_id}{ext}"

    def upload_source_md(self, markdown: str, file_id: str) -> str:
        self.md.append((file_id, markdown))
        return f"gs://nb/{file_id}.md"

    def upload_bytes(self, data: bytes, bucket: str, blob_name: str, content_type=None) -> str:
        self.blobs.append(blob_name)
        return f"gs://{bucket}/{blob_name}"

    def upload_source_sidecar_md(self, markdown: str, file_id: str) -> str:
        self.sidecars.append(file_id)
        return f"gs://nb/{file_id}.meta.md"

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)

    def download_bytes(self, uri: str) -> bytes:
        return self.objects[uri]


class FakeStore:
    def __init__(self) -> None:
        self.states: dict[str, Any] = {}
        self.split_queue: list[tuple[str, str, int]] = []
        self.dlq: list[tuple[str, str]] = []

    def get(self, file_id: str) -> Any:
        return self.states.get(file_id)

    def upsert(self, state: Any) -> None:
        self.states[state.file_id] = state

    def should_skip_reindex(self, file_id: str, content_hash: str) -> bool:
        st = self.states.get(file_id)
        return bool(
            st and st.content_hash == content_hash and st.status == DocStatus.INDEXED
        )

    def should_reparse(self, file_id: str, modified_time: Any) -> bool:
        return True

    def touch_modified_time(self, file_id: str, modified_time: Any) -> None:
        pass

    def enqueue_split(self, file_id: str, reason: str, size_bytes: int, **kw: Any) -> None:
        self.split_queue.append((file_id, reason, size_bytes))

    def enqueue_dlq(self, file_id: str, reason: str, **kw: Any) -> None:
        self.dlq.append((file_id, reason))


@dataclass
class _Resp:
    payload: dict
    status_code: int = 200
    headers: dict = None  # type: ignore[assignment]

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        pass

    @property
    def text(self) -> str:
        return json.dumps(self.payload)


class ParserStub:
    def __init__(self, blobs: dict[str, bytes], settings: Settings, gcs: FakeGcs) -> None:
        self.blobs = blobs
        self.settings = settings
        self.gcs = gcs
        self.calls = 0
        self.warnings: collections.Counter[str] = collections.Counter()

    def __enter__(self) -> ParserStub:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, headers=None, json=None):  # noqa: A002
        self.calls += 1
        file_id = json["fileId"]
        mime = json["mimeType"]
        filename = f"{file_id}.hwpx" if "hwpx" in mime.lower() else f"{file_id}.hwp"
        parsed = parse_document_bytes(self.blobs[file_id], filename=filename)
        markdown = cleanup_markdown(parsed.markdown)
        parsed.metrics.text_length = len(markdown)
        parsed.metrics.tables_rendered = count_markdown_tables(markdown)
        gate = evaluate_quality(parsed.metrics, self.settings)
        for w in parsed.metrics.warnings:
            self.warnings[w.split(":")[0]] += 1
        for r in gate.reasons:
            self.warnings[r.split(":")[0]] += 1

        md_uri = f"gs://nb/{file_id}.md"
        self.gcs.objects[md_uri] = markdown.encode("utf-8")
        return _Resp(
            {
                "gcsMarkdownUri": md_uri,
                "route": "HWPX" if parsed.engine == "python-hwpx" else "RHWP",
                "contentHash": sha256_text(markdown),
                "tableCount": parsed.metrics.table_count,
                "warnings": list(parsed.metrics.warnings) + gate.reasons,
                "textLength": len(markdown),
                "engine": parsed.engine,
            },
            headers={},
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="코퍼스를 실제 ingest 경로로 흘려 검증")
    ap.add_argument("corpus", help="파일이 든 디렉터리 (재귀 탐색)")
    ap.add_argument("--max-gcs-bytes", type=int, default=None)
    args = ap.parse_args()
    max_gcs_bytes = (
        args.max_gcs_bytes
        or Settings.__dataclass_fields__["max_gcs_bytes"].default
    )

    root = Path(args.corpus)
    if not root.is_absolute():
        root = ROOT / root
    files = {
        f"f{i:04d}": p
        for i, p in enumerate(sorted(p for p in root.rglob("*") if p.is_file()))
    }
    if not files:
        print(f"파일 없음: {root}", file=sys.stderr)
        return 1
    print(f"코퍼스 {root} — {len(files)}건, MAX_GCS_BYTES={max_gcs_bytes:,}\n")

    settings = Settings(
        gcp_project_id="p",
        gcs_hwp_original_bucket="rb",
        gcs_source_bucket="nb",
        rag_corpus_name="projects/p/locations/asia-northeast3/ragCorpora/c",
        max_gcs_bytes=max_gcs_bytes,
        qg_density_threshold=0.0005,
        qg_mode="log",
    )
    blobs: dict[str, bytes] = {}
    drive, gcs, store = FakeDrive(files, root), FakeGcs(), FakeStore()
    parser = ParserStub(blobs, settings, gcs)

    class _Httpx:
        @staticmethod
        def Client(**kw: Any) -> ParserStub:  # noqa: N802
            return parser

    sync_main.httpx = _Httpx
    sync_main._cloud_run_auth_headers = lambda url: {}

    by_ext: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    problems: list[tuple[str, str, str]] = []
    for fid, path in files.items():
        ext = path.suffix.lower()
        blobs[fid] = path.read_bytes()
        body = sync_main.IngestBody(
            fileId=fid,
            driveId="d1",
            name=path.name,
            mimeType=EXT_MIME.get(ext, "application/octet-stream"),
            sizeBytes=path.stat().st_size,
            parserUrl="http://parser",
            modifiedTime="2026-01-01T00:00:00Z",
        )
        try:
            res = sync_main._ingest_with(
                body, store=store, settings=settings, gcs=gcs, drive=drive
            )
        except Exception as exc:  # noqa: BLE001
            by_ext[ext]["예외"] += 1
            problems.append((path.name, "예외", f"{type(exc).__name__}: {exc}"[:100]))
            continue
        status = res.get("status", "?")
        by_ext[ext][status] += 1
        if status in ("SPLIT_QUEUED", "DLQ", "FAILED"):
            problems.append((path.name, status, str(res.get("error"))[:90]))

    print(f"{'확장자':<9} {'건수':>4}  상태 분포")
    print("-" * 76)
    for ext in sorted(by_ext, key=lambda e: -sum(by_ext[e].values())):
        print(f"{ext:<9} {sum(by_ext[ext].values()):>4}  {dict(by_ext[ext])}")

    print("\n=== GCS 업로드 ===")
    for label, n in (
        ("원본(raw)", len(gcs.raw)),
        ("정규화 md", len(gcs.md)),
        ("바이너리 복사", len(gcs.blobs)),
        ("경로 사이드카", len(gcs.sidecars)),
        ("파서 호출", parser.calls),
        ("Drive 다운로드", len(drive.downloads)),
    ):
        print(f"  {label:<16} {n}")

    if parser.warnings:
        print("\n=== 파서 경고 ===")
        for k, n in parser.warnings.most_common():
            print(f"  {k:<32} {n}")

    print("\n=== 검색 가능성 ===")
    print(f"  {dict(collections.Counter(s.status.value for s in store.states.values()))}")
    invisible = [
        s for s in store.states.values()
        if s.status in (DocStatus.SKIPPED, DocStatus.EXCLUDED)
    ]
    print(f"  검색 불가(SKIPPED/EXCLUDED) {len(invisible)}건")
    for s in invisible:
        print(f"      {s.name[:58]:<58} {s.mime_type}")
    nobody = [s.name for s in store.states.values()
              if (s.error or "").startswith("NO_BODY_EXTRACTOR")]
    print(f"  사이드카만(본문 없음) {len(nobody)}건")

    print(f"\n=== 문제 {len(problems)}건 ===")
    for name, kind, detail in problems:
        print(f"  [{kind}] {name[:52]}  {detail}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
