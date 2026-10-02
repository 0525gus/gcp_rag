"""PNG/JPEG의 텍스트만 추출한다. 이미지 설명 생성·표 복원·청킹은 하지 않는다."""

from __future__ import annotations

import io

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from PIL import Image, UnidentifiedImageError

from shared.config import Settings
from shared.mime_types import IMAGE_OCR_MAX_BYTES, IMAGE_OCR_MAX_PIXELS, IMAGE_OCR_MIME


class ImageOcrError(ValueError):
    pass


def validate_image(data: bytes, mime_type: str) -> None:
    if mime_type not in IMAGE_OCR_MIME:
        raise ImageOcrError("OCR_UNSUPPORTED_TYPE: only PNG/JPEG are enabled")
    if not data or len(data) > IMAGE_OCR_MAX_BYTES:
        raise ImageOcrError("OCR_SIZE_LIMIT: image must be nonempty and at most 10MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            expected = "PNG" if mime_type == "image/png" else "JPEG"
            if img.format != expected or getattr(img, "n_frames", 1) != 1:
                raise ImageOcrError("OCR_INVALID_IMAGE: MIME mismatch or multiple frames")
            if img.width * img.height > IMAGE_OCR_MAX_PIXELS:
                raise ImageOcrError("OCR_PIXEL_LIMIT: at most 40 megapixels")
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageOcrError("OCR_INVALID_IMAGE: unreadable PNG/JPEG") from exc


def extract_image_text(data: bytes, mime_type: str, settings: Settings) -> str:
    validate_image(data, mime_type)
    if not settings.docai_ocr_processor_id or not settings.docai_ocr_location:
        raise ImageOcrError("OCR_NOT_CONFIGURED: processor ID and location are required")
    client = documentai.DocumentProcessorServiceClient(client_options=ClientOptions(
        api_endpoint=f"{settings.docai_ocr_location}-documentai.googleapis.com",
    ))
    name = client.processor_path(
        settings.gcp_project_id, settings.docai_ocr_location, settings.docai_ocr_processor_id,
    )
    # 잘못 연결된 Layout Parser에 이미지를 보내거나 다른 처리 비용을 발생시키지 않는다.
    processor = client.get_processor(name=name, retry=None, timeout=30)
    if processor.type_ != "OCR_PROCESSOR":
        raise ImageOcrError("OCR_WRONG_PROCESSOR: OCR_PROCESSOR is required")
    result = client.process_document(
        request=documentai.ProcessRequest(
            name=name, raw_document=documentai.RawDocument(content=data, mime_type=mime_type),
        ),
        retry=None, timeout=120,
    )
    text = (result.document.text or "").strip()
    if not text:
        raise ImageOcrError("OCR_EMPTY_TEXT: no text extracted; nothing will be indexed")
    return text
