"""Image OCR opt-in, validation, routing and RAG handoff; no paid API calls."""

import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from services.parser import image_ocr
from services.parser import main as parser
from services.sync import main as sync
from shared.config import Settings, _departments_from_json
from shared.mime_types import IMAGE_OCR_MAX_BYTES, RouteKind, classify_route
from shared.models import Audience, DocState, DocStatus, DriveChange, ParseRoute
from shared.path_context import build_path_context
from shared.rag_engine import ImportOutcome, RagEngineClient


def image_bytes(fmt="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(stream, format=fmt)
    return stream.getvalue()


@pytest.mark.parametrize("mime,fmt", [("image/png", "PNG"), ("image/jpeg", "JPEG")])
def test_valid_image_calls_only_ocr_processor(mime, fmt, monkeypatch):
    client = MagicMock()
    client.processor_path.return_value = "projects/p/locations/us/processors/ocr"
    client.get_processor.return_value = SimpleNamespace(type_="OCR_PROCESSOR")
    client.process_document.return_value = SimpleNamespace(document=SimpleNamespace(text="  안내문\n접수 기한 10월 20일  "))
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(image_ocr.documentai, "DocumentProcessorServiceClient", factory)
    settings = Settings(gcp_project_id="p", docai_ocr_location="us", docai_ocr_processor_id="ocr")
    data = image_bytes(fmt)
    assert image_ocr.extract_image_text(data, mime, settings) == "안내문\n접수 기한 10월 20일"
    request = client.process_document.call_args.kwargs["request"]
    assert request.raw_document.mime_type == mime
    assert request.raw_document.content == data
    assert client.process_document.call_args.kwargs["retry"] is None
    assert factory.call_args.kwargs["client_options"].api_endpoint == "us-documentai.googleapis.com"


@pytest.mark.parametrize("case", ["wrong-processor", "empty", "missing-config"])
def test_invalid_ocr_setup_and_empty_response_fail(case, monkeypatch):
    client = MagicMock()
    client.processor_path.return_value = "projects/p/locations/us/processors/ocr"
    client.get_processor.return_value = SimpleNamespace(type_=(
        "LAYOUT_PARSER_PROCESSOR" if case == "wrong-processor" else "OCR_PROCESSOR"
    ))
    client.process_document.return_value = SimpleNamespace(document=SimpleNamespace(text=" \n"))
    monkeypatch.setattr(image_ocr.documentai, "DocumentProcessorServiceClient", lambda **kw: client)
    settings = Settings(gcp_project_id="p", docai_ocr_location="us", docai_ocr_processor_id="ocr")
    if case == "missing-config":
        settings = replace(settings, docai_ocr_location="")
    with pytest.raises(image_ocr.ImageOcrError):
        image_ocr.extract_image_text(image_bytes(), "image/png", settings)
    if case != "empty":
        client.process_document.assert_not_called()


@pytest.mark.parametrize("data,mime", [
    (b"not an image", "image/png"), (b"", "image/png"),
    (image_bytes(), "image/jpeg"), (image_bytes(), "image/webp"),
    (b"x" * (IMAGE_OCR_MAX_BYTES + 1), "image/png"),
], ids=["invalid", "empty", "mime-mismatch", "unsupported", "oversize"])
def test_invalid_input_is_rejected_before_any_cloud_client(data, mime, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(image_ocr.documentai, "DocumentProcessorServiceClient", factory)
    with pytest.raises(image_ocr.ImageOcrError):
        image_ocr.extract_image_text(data, mime, Settings(gcp_project_id="p"))
    factory.assert_not_called()


def test_pixel_limit(monkeypatch):
    monkeypatch.setattr(image_ocr, "IMAGE_OCR_MAX_PIXELS", 99)
    with pytest.raises(image_ocr.ImageOcrError, match="OCR_PIXEL_LIMIT"):
        image_ocr.validate_image(image_bytes(), "image/png")


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_images_require_opt_in_and_deletion_always_wins(mime):
    assert classify_route(mime, "a.png") == RouteKind.SKIP
    assert classify_route(mime, "a.png", enable_image_ocr=True) == RouteKind.IMAGE_OCR
    assert classify_route(mime, "a.png", removed=True, enable_image_ocr=True) == RouteKind.DELETE
    assert classify_route("application/octet-stream", "a.png", enable_image_ocr=True) == RouteKind.SKIP
    assert classify_route("image/webp", "a.webp", enable_image_ocr=True) == RouteKind.SKIP


def test_department_opt_in_is_isolated():
    data = {code: {"driveIds": [code], "staffCorpus": code, "syncFolderIds": [code],
                   "hwpBucket": "raw", "sourceBucket": "source"} for code in ("cs", "ee")}
    data["cs"]["enableImageOcr"] = True
    settings = Settings(gcp_project_id="p", departments=_departments_from_json(json.dumps(data)))
    assert settings.for_drive("cs").enable_image_ocr
    assert not settings.for_drive("cs").for_drive("ee").enable_image_ocr
    assert not settings.enable_image_ocr
    data["ee"]["enableImageOcr"] = "false"
    with pytest.raises(ValueError, match="enableImageOcr"):
        _departments_from_json(json.dumps(data))


@pytest.fixture
def ingest(monkeypatch):
    settings = Settings(gcp_project_id="p", enable_image_ocr=True,
                        gcs_hwp_original_bucket="cs-raw", gcs_source_bucket="cs-source")
    store, drive, gcs = MagicMock(), MagicMock(), MagicMock()
    store.should_skip_reindex.return_value = False
    store.should_reparse.return_value = True
    store.get.return_value = None
    drive.download_file.return_value = image_bytes()
    drive.resolve_path_context.return_value = build_path_context(["학과", "공지"], "안내.png")
    gcs.upload_bytes.return_value = "gs://cs-raw/doc.png"
    gcs.upload_source_md.return_value = "gs://cs-source/doc.md"
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.status_code = 200
    client.post.return_value.json.return_value = {"text": "접수 기한 10월 20일", "route": "IMAGE_DOCAI"}
    monkeypatch.setattr(sync.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(sync, "_cloud_run_auth_headers", lambda _: {"Authorization": "Bearer fake"})
    body = sync.IngestBody(fileId="doc", driveId="cs", name="안내.png", mimeType="image/png",
                           parserUrl="https://parser.example", webViewLink="https://drive.google.com/file/d/doc")
    return SimpleNamespace(settings=settings, store=store, drive=drive, gcs=gcs, client=client, body=body)


def run_ingest(ctx):
    return sync._ingest_locked(ctx.body, store=ctx.store, settings=ctx.settings, gcs=ctx.gcs, drive=ctx.drive)


def test_only_ocr_markdown_reaches_existing_rag_chunker(ingest, monkeypatch):
    result = run_ingest(ingest)
    assert result["status"] == "GCS_READY"
    assert result["gcsUris"] == ["gs://cs-source/doc.md"]
    assert ingest.client.post.call_args.args[0].endswith("/ocr")
    payload = ingest.client.post.call_args.kwargs["json"]
    assert payload["enableImageOcr"] is True
    assert payload["gcsUri"] == "gs://cs-raw/doc.png"
    ingest.drive.download_file.assert_called_once_with("doc", max_bytes=IMAGE_OCR_MAX_BYTES)
    markdown = ingest.gcs.upload_source_md.call_args.args[0]
    assert "접수 기한 10월 20일" in markdown and "안내.png" in markdown
    saved = ingest.store.upsert.call_args.args[0]
    assert saved.parse_route == ParseRoute.IMAGE_DOCAI and saved.status == DocStatus.PARSED
    assert saved.audience == Audience.STAFF
    assert saved.source_uri == ingest.body.web_view_link
    rag = object.__new__(RagEngineClient)
    rag.settings = ingest.settings
    capture = MagicMock(return_value=ImportOutcome(uris=result["gcsUris"], imported=1, failed=0, skipped=0))
    monkeypatch.setattr(rag, "_import_batch", capture)
    monkeypatch.setattr(rag, "_mark_dirty", lambda _: None)
    rag.import_from_gcs(result["gcsUris"])
    uris, transformation, _ = capture.call_args.args
    assert uris == ["gs://cs-source/doc.md"]
    assert transformation.chunking_config.chunk_size == 1024
    assert transformation.chunking_config.chunk_overlap == 256


@pytest.mark.parametrize("status,text", [(503, ""), (422, ""), (200, "   ")])
def test_failed_or_empty_ocr_never_writes_indexable_markdown(ingest, status, text):
    ingest.client.post.return_value.status_code = status
    ingest.client.post.return_value.json.return_value = {"text": text}
    result = run_ingest(ingest)
    assert result["status"] == "DLQ"
    assert "gcsUris" not in result
    ingest.gcs.upload_source_md.assert_not_called()
    ingest.store.upsert.assert_not_called()
    ingest.store.enqueue_dlq.assert_called_once()


def test_disabled_ocr_ignores_stale_queue_route_without_downloading(ingest):
    ingest.settings = replace(ingest.settings, enable_image_ocr=False)
    ingest.body.route = "IMAGE_OCR"
    ingest.store.get.return_value = DocState(file_id="doc", drive_id="cs", status=DocStatus.INDEXED)
    assert run_ingest(ingest)["status"] == "SKIPPED"
    ingest.drive.download_file.assert_not_called()
    ingest.client.post.assert_not_called()
    ingest.store.upsert.assert_not_called()


def test_oversize_image_is_not_downloaded(ingest):
    ingest.body.size_bytes = IMAGE_OCR_MAX_BYTES + 1
    assert run_ingest(ingest)["status"] == "DLQ"
    ingest.drive.download_file.assert_not_called()
    ingest.client.post.assert_not_called()


def test_unchanged_ocr_preserves_existing_indexed_state(ingest):
    ingest.store.should_skip_reindex.return_value = True
    assert run_ingest(ingest)["status"] == "HASH_UNCHANGED"
    ingest.gcs.upload_source_md.assert_not_called()
    ingest.store.upsert.assert_not_called()
    ingest.store.touch_modified_time.assert_called_once()


@pytest.mark.parametrize("membership,expected", [
    (True, Audience.STUDENT),
    (False, Audience.STAFF),
    (RuntimeError("parent lookup unavailable"), Audience.STAFF),
])
def test_ocr_student_visibility_requires_confirmed_folder_membership(ingest, membership, expected):
    ingest.settings = replace(ingest.settings, student_folder_ids="student-folder",
                              rag_corpus_name_student="corpora/student")
    if isinstance(membership, Exception):
        ingest.drive.is_in_sync_scope.side_effect = membership
    else:
        ingest.drive.is_in_sync_scope.return_value = membership
    assert run_ingest(ingest)["status"] == "GCS_READY"
    assert ingest.store.upsert.call_args.args[0].audience == expected
    ingest.drive.is_in_sync_scope.assert_called_once_with("doc", ["student-folder"])


@pytest.mark.parametrize("enabled", [False, True])
def test_delta_and_backfill_share_image_policy(ingest, monkeypatch, enabled):
    settings = replace(ingest.settings, enable_image_ocr=enabled)
    ingest.store.get_start_page_token.return_value = "token"
    ingest.drive.iter_backfill_files.return_value = iter([
        {"id": "doc", "name": "a.png", "mimeType": "image/png"},
    ])
    ingest.drive.list_changes.return_value = ([DriveChange(file_id="doc", drive_id="cs", mime_type="image/png")], "next", False)
    monkeypatch.setattr(sync, "DocStateStore", lambda: ingest.store)
    monkeypatch.setattr(sync, "DriveClient", lambda: ingest.drive)
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    delta = sync.list_changes(sync.ChangesBody(driveId="cs"))
    assert delta["changes"][0]["route"] == ("IMAGE_OCR" if enabled else "SKIP")
    backfill = sync._build_backfill_changes("cs", store=ingest.store, drive=ingest.drive, settings=settings)
    assert len(backfill["changes"]) == int(enabled)


def test_parser_ocr_requires_explicit_opt_in_and_caps_download(monkeypatch):
    settings = Settings(gcp_project_id="p", docai_ocr_location="us", docai_ocr_processor_id="ocr")
    monkeypatch.setattr(parser, "get_settings", lambda: settings)
    gcs = MagicMock()
    blob = gcs._client.bucket.return_value.get_blob.return_value
    blob.size = 100
    blob.download_as_bytes.return_value = image_bytes()
    monkeypatch.setattr(parser, "GcsClient", lambda _: gcs)
    extract = MagicMock(return_value="정상 OCR 본문")
    monkeypatch.setattr(image_ocr, "extract_image_text", extract)
    client = TestClient(parser.app)
    payload = {"gcsUri": "gs://cs-raw/doc.png", "fileId": "doc", "mimeType": "image/png"}
    assert client.post("/ocr", json=payload).status_code == 403
    extract.assert_not_called()
    payload["enableImageOcr"] = True
    response = client.post("/ocr", json=payload)
    assert response.status_code == 200 and response.json()["text"] == "정상 OCR 본문"
    blob.download_as_bytes.assert_called_once_with(end=IMAGE_OCR_MAX_BYTES)
    gcs.upload_source_md.assert_not_called()
    blob.size = IMAGE_OCR_MAX_BYTES + 1
    assert client.post("/ocr", json=payload).status_code == 413
    assert extract.call_count == 1


def test_drive_download_stops_at_cap(monkeypatch):
    from shared import drive as drive_module

    def downloader(buffer, request, chunksize):
        def next_chunk(**kwargs):
            buffer.write(b"x" * 11)
            return None, False
        return SimpleNamespace(next_chunk=next_chunk)
    monkeypatch.setattr(drive_module, "MediaIoBaseDownload", downloader)
    drive = object.__new__(drive_module.DriveClient)
    drive._service = MagicMock()
    with pytest.raises(ValueError, match="DOWNLOAD_SIZE_EXCEEDED"):
        drive.download_file("doc", max_bytes=10)
