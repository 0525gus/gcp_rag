"""텍스트 클린업: NFC 정규화 + 머리말/꼬리말·페이지번호 잡음 제거."""

from __future__ import annotations

import re
import unicodedata


# 반복 페이지 번호 / 머리말 패턴
_PAGE_NO = re.compile(
    r"(?m)^\s*(?:-?\s*)?(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,6})\s*(?:-?\s*)?$"
)
_HEADER_FOOTER_NOISE = re.compile(
    r"(?m)^\s*(?:Page\s+\d+(?:\s+of\s+\d+)?|페이지\s*\d+\s*/\s*\d+)\s*$"
)
_MULTI_BLANK = re.compile(r"\n{3,}")


def to_nfc(text: str) -> str:
    """한글 자모 분리(NFD) → NFC 통일 (임베딩 정합성)."""
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