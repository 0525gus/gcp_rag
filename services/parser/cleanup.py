from __future__ import annotations

import re
import unicodedata


_ROMAN = r"(?=[mdclxvi])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_NUM = rf"(?:\d{{1,4}}|{_ROMAN})"
_PAGE_NO = re.compile(
    rf"(?im)^\s*(?:"
    rf"-\s*{_NUM}\s*-?"
    rf"|{_NUM}\s*-"
    rf"|\d{{1,3}}"
    rf"|{_ROMAN}"
    rf")\s*$"
)
_HEADER_FOOTER_NOISE = re.compile(
    r"(?m)^\s*(?:Page\s+\d+(?:\s+of\s+\d+)?|페이지\s*\d+\s*/\s*\d+)\s*$"
)
_MULTI_BLANK = re.compile(r"\n{3,}")


def to_nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def strip_noise(text: str) -> str:
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        if _PAGE_NO.match(line) or _HEADER_FOOTER_NOISE.match(line):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned)
    result = _MULTI_BLANK.sub("\n\n", result)
    return result.strip()


def cleanup_markdown(text: str) -> str:
    return strip_noise(to_nfc(text))