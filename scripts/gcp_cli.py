from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# gcloud 는 남은 수명이 300초 이하인 토큰만 갱신해서 준다(core/credentials/store.py
# _CREDENTIALS_EXPIRY_WINDOW). 받은 토큰은 최소 300초 유효하므로 그보다 짧게 쓴다.
ACCESS_TOKEN_TTL_SECONDS = 240

_token_lock = threading.Lock()
_token = ""
_token_expires = 0.0


def run_command(args: list[str], timeout: int = 12) -> tuple[bool, str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output


def gcloud_install_locations() -> list[Path]:
    """Cloud SDK 설치 프로그램의 기본 경로. 설치 직후 이 서버 프로세스의 PATH는
    갱신되지 않으므로, 콘솔에서 설치한 gcloud를 재시작 없이 찾으려면 직접 본다."""
    if os.name != "nt":
        return []
    bases = [os.environ.get(name) for name in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles")]
    return [
        Path(base) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"
        for base in bases
        if base
    ]


def gcloud_executable() -> str | None:
    """Windows에서는 CreateProcess가 PATHEXT shim을 해석하지 못하므로 .cmd를 고정."""
    if os.name == "nt":
        found = shutil.which("gcloud.cmd") or shutil.which("gcloud.CMD")
        if found:
            return found
        return next((str(path) for path in gcloud_install_locations() if path.is_file()), None)
    return shutil.which("gcloud")


def gcloud_json(args: list[str], timeout: int = 12) -> tuple[bool, Any]:
    executable = gcloud_executable()
    if not executable:
        return False, "gcloud를 찾을 수 없습니다."
    ok, output = run_command([executable, *args, "--format=json", "--quiet"], timeout)
    if not ok:
        return False, output
    try:
        return True, json.loads(output or "{}")
    except json.JSONDecodeError:
        return False, "gcloud 응답이 JSON이 아닙니다."


def access_token() -> str:
    global _token, _token_expires
    with _token_lock:
        if _token and time.monotonic() < _token_expires:
            return _token
        gcloud = gcloud_executable()
        if not gcloud:
            raise RuntimeError("gcloud를 찾을 수 없습니다.")
        ok, token = run_command([gcloud, "auth", "print-access-token", "--quiet"], timeout=20)
        if not ok or not token.strip():
            raise RuntimeError("gcloud 로그인이 필요합니다.")
        _token = token.strip()
        _token_expires = time.monotonic() + ACCESS_TOKEN_TTL_SECONDS
        return _token


def forget_access_token() -> None:
    global _token, _token_expires
    with _token_lock:
        _token, _token_expires = "", 0.0


def _http(
    method: str, url: str, payload: dict[str, Any] | None, token: str, timeout: int
) -> tuple[int, Any, int]:
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
            elapsed = round((time.perf_counter() - started) * 1000)
            try:
                return response.status, json.loads(raw or "{}"), elapsed
            except json.JSONDecodeError:
                return response.status, {}, elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read(1024 * 1024).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        return exc.code, body, round((time.perf_counter() - started) * 1000)
    except (OSError, TimeoutError):
        return 0, {}, round((time.perf_counter() - started) * 1000)


def http_json(url: str, token: str = "", timeout: int = 10) -> tuple[int, Any, int]:
    return _http("GET", url, None, token, timeout)


def http_post_json(
    url: str, payload: dict[str, Any], token: str = "", timeout: int = 10
) -> tuple[int, Any, int]:
    return _http("POST", url, payload, token, timeout)


def http_delete_json(url: str, token: str = "", timeout: int = 10) -> tuple[int, Any, int]:
    return _http("DELETE", url, None, token, timeout)
