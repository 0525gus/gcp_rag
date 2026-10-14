from dataclasses import replace
from unittest.mock import MagicMock
import json

import pytest

from shared.post_metadata import parse_post_metadata, METADATA_MAX_BYTES
from shared.drive import DriveClient
from shared.models import DocState, DocStatus
from shared.path_context import build_path_context, build_breadcrumb_markdown
from shared.search_postprocess import citation_label
from services.sync import main as sync


def test_metadata_validates_two_fields_and_ignores_extra_fields():
    metadata = parse_post_metadata(
        json.dumps(
            {
                "title": " 게시글 제목 ",
                "source_url": "https://example.org/posts/153759",
                "audience": "STUDENT",
            }
        ).encode()
    )
    assert metadata.title == "게시글 제목"
    assert metadata.source_url == "https://example.org/posts/153759"
    assert parse_post_metadata(b"{}").title == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"broken",
        b'{"title":42}',
        b'{"source_url":"javascript:alert(1)"}',
        b'{"source_url":"https://user:pass@example.org"}',
        b'{"source_url":"https://example.org:invalid"}',
        b'{"title":"a\\nb"}',
        b" " * (METADATA_MAX_BYTES + 1),
    ],
    ids=[
        "array",
        "json",
        "title-type",
        "scheme",
        "credentials",
        "port",
        "title-control",
        "oversized",
    ],
)
def test_invalid_metadata_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_post_metadata(payload)


def drive_with_metadata(rows, payload=b'{"title":"post","source_url":"https://example.org/post"}'):
    drive = object.__new__(DriveClient)
    drive._service = MagicMock()
    drive.get_parents = MagicMock(return_value=["folder"])
    drive._service.files.return_value.list.return_value.execute.return_value = {"files": rows}
    drive.download_file = MagicMock(return_value=payload)
    return drive


def test_drive_reads_only_direct_parent_and_limits_sidecar_download():
    drive = drive_with_metadata([{"id": "metadata", "mimeType": "application/json", "size": "80"}])
    metadata, file_id = drive.read_post_metadata("document")
    assert metadata.title == "post" and file_id == "metadata"
    assert "'folder' in parents" in drive._service.files.return_value.list.call_args.kwargs["q"]
    drive.download_file.assert_called_once_with("metadata", max_bytes=METADATA_MAX_BYTES)


@pytest.mark.parametrize(
    "rows", [[], [{"id": "a"}, {"id": "b"}], [{"id": "a", "size": str(METADATA_MAX_BYTES + 1)}]]
)
def test_absent_ambiguous_or_oversized_sidecars_never_download(rows):
    drive = drive_with_metadata(rows)
    if not rows:
        assert drive.read_post_metadata("document")[1] == ""
    else:
        with pytest.raises(ValueError):
            drive.read_post_metadata("document")
    drive.download_file.assert_not_called()


def test_metadata_preserves_original_identity_and_breadcrumb_is_idempotent():
    context = replace(
        build_path_context(["posts", "153759"], "poster.png"),
        source_title="Post title",
        website_url="https://example.org/post",
        metadata_file_id="meta",
    )
    args = dict(
        path=context.path,
        bundle=context.bundle,
        title="poster.png",
        source_title=context.source_title,
        website_url=context.website_url,
    )
    first = build_breadcrumb_markdown(**args, body="OCR content")
    assert first.startswith("# Post title")
    assert "파일: poster.png" in first and "https://example.org/post" in first
    assert build_breadcrumb_markdown(**args, body=first) == first
    state = sync._state_fields(
        sync.IngestBody(fileId="file", driveId="drive", name="poster.png"),
        content_hash="hash",
        status=DocStatus.PARSED,
        parse_route=sync.ParseRoute.IMAGE_DOCAI,
        source_uri="https://drive.google.com/file/d/file/view",
        path_ctx=context,
    )
    saved = DocState.from_firestore(state.to_firestore())
    assert saved.name == "poster.png" and saved.path == "posts/153759/poster.png"
    assert saved.source_title == "Post title" and saved.website_url == "https://example.org/post"
    assert saved.source_uri.startswith("https://drive.google.com/")
    assert (
        citation_label({"title": saved.source_title, "name": saved.name})
        == "Post title / poster.png"
    )


def test_metadata_file_never_enters_initial_listing_or_ingest(monkeypatch):
    drive, store, gcs = MagicMock(), MagicMock(), MagicMock()
    store.get.return_value = None
    assert (
        sync._route_file_meta(
            drive_id="drive",
            file_meta={"id": "meta", "name": "metadata.json", "mimeType": "application/json"},
            folder_ids=[],
            drive=drive,
        )
        is None
    )
    result = sync._ingest_locked(
        sync.IngestBody(
            fileId="meta",
            driveId="drive",
            name="metadata.json",
            mimeType="application/json",
            route="FILE_COPY",
        ),
        store=store,
        gcs=gcs,
        drive=drive,
        settings=sync.Settings(gcp_project_id="project"),
    )
    assert result["status"] == "EXCLUDED"
    drive.download_file.assert_not_called()
    gcs.upload_source_md.assert_not_called()


def test_gui_enriches_every_chunk_with_the_same_post_and_preserves_drive_link(monkeypatch):
    from scripts import dept_gui

    fid = "1fAIvFNOKtIIoy3RdP-Vrc9XEGV3nzyoa"
    snap = MagicMock()
    snap.id, snap.exists = fid, True
    snap.to_dict.return_value = {
        "driveId": "drive",
        "audience": "STUDENT",
        "sourceTitle": "Post",
        "websiteUrl": "https://example.org/post",
        "sourceUri": "https://drive.google.com/file/d/" + fid + "/view",
        "name": "poster.png",
    }
    db = MagicMock()
    db.get_all.return_value = [snap]
    monkeypatch.setattr(dept_gui, "_firestore_client", lambda _: db)
    config = {"buckets": {"source": "source"}, "drive": {"driveIds": ["drive"]}}
    raw = [{"sourceUri": f"gs://source/{fid}.md"}, {"sourceUri": f"gs://source/{fid}.meta.md"}]
    result = dept_gui._corpus_post_metadata(raw, config, {"GCP_PROJECT_ID": "project"}, "student")
    assert result[fid]["websiteUrl"] == "https://example.org/post"
    assert result[fid]["driveUri"].startswith("https://drive.google.com/")
    assert len(db.get_all.call_args.args[0]) == 1
    snap.to_dict.return_value["audience"] = "STAFF"
    assert (
        dept_gui._corpus_post_metadata(raw, config, {"GCP_PROJECT_ID": "project"}, "student") == {}
    )
