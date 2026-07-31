"""로컬 스크립트용 `.env` 로더.

서비스는 Cloud Run 환경변수로 설정을 받지만, 로컬에서 돌리는 스크립트는
저장소 루트의 `.env` 를 읽는 편이 낫다. 그러지 않으면 `GCP_PROJECT_ID` 부터
`RAG_CORPUS_NAME` 까지 대여섯 개를 매번 손으로 넣어야 한다.

`shared/` 가 아니라 `scripts/` 에 두는 이유: `.env` 를 읽는 동작은 로컬 전용이고
`.gcloudignore` 가 `.env` 를 빼므로 컨테이너 안에서는 존재하지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    """`.env` 를 os.environ 에 채운다. 이미 있는 값은 덮지 않는다.

    setdefault 인 이유: 명령줄에서 준 값이 파일보다 우선해야 한다.
    """
    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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
