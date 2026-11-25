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
