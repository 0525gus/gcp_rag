"""텍스트 클린업: NFC 정규화 + 머리말/꼬리말·페이지번호 잡음 제거."""

from __future__ import annotations

import re
import unicodedata


# 반복 페이지 번호 / 머리말 패턴
# - 대시 장식형("- 3 -", "— iv —")은 페이지 번호로 확정 → 1~4자리 허용
# - 장식 없는 숫자는 1~3자리만 제거 (연도 2024·금액 등 본문 오탐 방지)
# - 로마숫자는 '유효한' 형태만 매칭 (did/civil/dim 등 알파벳 단어 오탐 방지)
_ROMAN = r"(?=[mdclxvi])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_NUM = rf"(?:\d{{1,4}}|{_ROMAN})"
_PAGE_NO = re.compile(
    rf"(?im)^\s*(?:"
    rf"-\s*{_NUM}\s*-?"  # 앞 대시(뒤 대시 선택): "- 3", "- 3 -", "— iv —"
    rf"|{_NUM}\s*-"  # 뒤 대시만: "3 -"
    rf"|\d{{1,3}}"  # 장식 없는 숫자: 1~3자리만
    rf"|{_ROMAN}"  # 장식 없는 유효 로마숫자
    rf")\s*$"
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