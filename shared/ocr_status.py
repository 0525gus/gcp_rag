"""Recognize the parser's explicit no-text outcome, without hiding other errors."""

import json

from shared.mime_types import IMAGE_OCR_MIME


def is_empty_ocr_error(reason, mime_type):
    if (mime_type or "").lower() not in IMAGE_OCR_MIME:
        return False
    reason = str(reason or "")
    if reason == "OCR_EMPTY_TEXT" or reason.startswith("OCR_EMPTY_TEXT:"):
        return True
    prefix = "IMAGE_OCR_FAILED:422:"
    if reason.startswith(prefix):
        try:
            detail = json.loads(reason[len(prefix) :]).get("detail")
        except (ValueError, AttributeError):
            return False
        return isinstance(detail, str) and (
            detail == "OCR_EMPTY_TEXT" or detail.startswith("OCR_EMPTY_TEXT:")
        )
    return False
