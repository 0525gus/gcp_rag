import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ATTACHMENT_SUFFIXES = (
    ".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def citation_links(
    file_uri: str | None,
    filename: str | None,
    website_url: str | None,
    *,
    prefer_post: bool,
) -> dict[str, str | None]:
    file_link = citation_view_uri(file_uri, filename)
    if not (prefer_post and website_url):
        return {"sourceUri": file_link}
    links: dict[str, str | None] = {"sourceUri": website_url}
    if file_link and (filename or "").strip().lower().endswith(_ATTACHMENT_SUFFIXES):
        links["attachmentUri"] = file_link
    return links


def citation_view_uri(uri: str | None, filename: str | None = None) -> str | None:
    if not uri:
        return uri
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri
    if parts.scheme != "https" or parts.netloc not in {
        "docs.google.com", "drive.google.com",
    }:
        return uri
    if parts.netloc == "docs.google.com":
        match = re.fullmatch(
            r"/(?:document|spreadsheets|presentation)/(?:u/\d+/)?d/([\w-]+)"
            r"(?:/(?:edit|view|preview))?/?",
            parts.path,
        )
    else:
        match = re.fullmatch(r"/file/d/([\w-]+)/(?:edit|view|preview)/?", parts.path)
    if not match:
        return uri
    resource_keys = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if key.lower() == "resourcekey"
    ]
    if (filename or "").strip().lower().endswith((".hwp", ".hwpx")):
        query = urlencode([
            ("export", "download"),
            ("id", match.group(1)),
            *resource_keys,
        ])
        return urlunsplit(("https", "drive.google.com", "/uc", query, ""))

    query = urlencode(resource_keys)
    return urlunsplit(("https", "drive.google.com", f"/file/d/{match.group(1)}/view", query, ""))
