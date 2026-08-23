"""로컬 스크립트 공용 유틸.

설정 로딩은 여기 없다 — `scripts/dept_config.load_config_env()` 가 config/ 를
읽는다. 예전에는 이 파일이 `.env` 로더를 들고 있었고, 그래서 로컬 도구와
배포가 서로 다른 원본을 봤다: 평가와 운영이 다른 파라미터로 측정됐다
(docs/ENV_MIGRATION.md).
"""

from __future__ import annotations


def force_utf8_stdout() -> None:
    """한국어 Windows 콘솔은 기본이 cp949 라 UTF-8 출력이 깨진다.

    한글 자체는 cp949 에 있어 통과하지만 `—`(em dash) 같은 문자가 섞이면
    UnicodeEncodeError 로 **스크립트가 죽는다**. 조회를 다 끝내고 마지막 출력에서
    죽어 아무것도 못 보는 일이 실제로 있었다. 리다이렉트된 경우도 같은 문제라
    stdout/stderr 양쪽을 바꾼다.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
