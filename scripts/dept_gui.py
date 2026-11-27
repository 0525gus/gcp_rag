from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.console import (
    core,
    corpus_chat,
    dept_forms,
    docai_routes,
    drive_lookup,
    environment_setup,
    issue_routes,
    provisioning,
    runtime_deploy,
    sa_repair,
    status_checks,
    sync_history,
    teardown_flow,
)

WEB_DIR = ROOT / "gui" / "public" / "console"

app = FastAPI(title="GCP RAG 학과 관리", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/api/v1/session")
def session() -> dict[str, Any]:
    return {"nonce": core._SESSION_NONCE, "commonExists": (core.CONFIG_DIR / "common.yaml").exists()}


app.include_router(issue_routes.router)
app.include_router(dept_forms.router)
app.include_router(provisioning.router)
app.include_router(runtime_deploy.router)
app.include_router(teardown_flow.router)
app.include_router(environment_setup.router)
app.include_router(sa_repair.router)
app.include_router(docai_routes.router)
app.include_router(corpus_chat.router)
app.include_router(drive_lookup.router)
app.include_router(sync_history.router)
app.include_router(status_checks.router)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/console", StaticFiles(directory=WEB_DIR), name="console")


def main() -> None:
    parser = argparse.ArgumentParser(description="GCP RAG 학과 관리 GUI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not WEB_DIR.is_dir():
        raise SystemExit(f"GUI asset directory not found: {WEB_DIR}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
