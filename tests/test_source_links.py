import pytest

from shared.source_links import citation_view_uri


@pytest.mark.parametrize("kind", ["document", "spreadsheets", "presentation"])
@pytest.mark.parametrize("suffix", ["/edit", "/view", "", "/preview"])
def test_google_editor_links_open_preview(kind, suffix):
    base = f"https://docs.google.com/{kind}/d/abc-123_"
    extra = "?resourcekey=access-key&gid=12#gid=12"
    assert citation_view_uri(base + suffix + extra) == "https://drive.google.com/file/d/abc-123_/view?resourcekey=access-key"


def test_account_scoped_editor_and_drive_edit_links():
    assert citation_view_uri("https://docs.google.com/document/u/0/d/abc/edit") == (
        "https://drive.google.com/file/d/abc/view"
    )
    assert citation_view_uri("https://drive.google.com/file/d/abc/edit") == (
        "https://drive.google.com/file/d/abc/view"
    )


@pytest.mark.parametrize("uri", [
    None, "", "gs://bucket/document.md", "https://example.com/edit",
    "https://docs.google.com.evil.test/document/d/abc/edit",
    "https://drive.google.com/file/d/abc/view?resourcekey=key",
    "https://docs.google.com/document/d/e/published-id/pub",
    "https://drive.google.com/drive/folders/abc", "https://[invalid",
])
def test_other_source_uris_remain_unchanged(uri):
    assert citation_view_uri(uri) == uri


def test_office_editor_uri_uses_drive_viewer_without_editor_options():
    uri = "https://docs.google.com/document/d/1ltVwKUmqihSeaol1eB_Qmg2K__Se-6TD/preview?usp=drivesdk&ouid=112918399556333167261&rtpof=true&sd=true"
    assert citation_view_uri(uri) == "https://drive.google.com/file/d/1ltVwKUmqihSeaol1eB_Qmg2K__Se-6TD/view"
