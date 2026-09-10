"""Presentation links for search citations; stored source identities stay intact."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def citation_view_uri(uri: str | None) -> str | None:
    """Route Google editor links through the Drive file viewer, preserving access keys."""
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
    # Editor launch/account options (rtpof, ouid, sd, etc.) do not belong in citations.
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                       if key.lower() == "resourcekey"])
    return urlunsplit(("https", "drive.google.com", f"/file/d/{match.group(1)}/view", query, ""))
