"""Google Drive Changes API (Shared Drives)."""

from __future__ import annotations

import io
import logging
from typing import Any, Iterator

from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from shared.folder_scope import is_under_folder_allowlist
from shared.models import DriveChange
from shared.path_context import PathContext, build_path_context

logger = logging.getLogger(__name__)

FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,trashed,md5Checksum,"
    # size 는 다운로드 '전에' 크기를 거르기 위해 필요하다. 받아 놓고 재면 이미
    # 메모리를 점유한 뒤라, 백필의 병렬 워커가 큰 파일을 동시에 물면 OOM 이다.
    # Google 네이티브(Docs/Sheets/Slides)는 blob 이 아니라서 이 필드가 없다.
    "size,webViewLink,parents,driveId"
)

# googleapiclient 내장 재시도: 429/5xx·연결 오류에 지수 백오프 (커스텀 로직 불필요)
NUM_RETRIES = 5


def parse_drive_size(raw: Any) -> int | None:
    """Drive 는 size 를 문자열로 준다. 없거나 이상하면 None(=크기 미상)."""
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


class DriveClient:
    def __init__(self) -> None:
        credentials, _ = google_auth_default(
            scopes=[
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
            ]
        )
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._parent_cache: dict[str, list[str]] = {}
        self._name_cache: dict[str, str] = {}

    def get_start_page_token(self, drive_id: str) -> str:
        resp = (
            self._service.changes()
            .getStartPageToken(driveId=drive_id, supportsAllDrives=True)
            .execute(num_retries=NUM_RETRIES)
        )
        return resp["startPageToken"]

    def list_changes(
        self, drive_id: str, page_token: str, *, max_changes: int | None = None
    ) -> tuple[list[DriveChange], str, bool]:
        """changes.list 페이지네이션. 반환: (변경 목록, 다음 토큰, 더 남았는지).

        ``max_changes`` 를 주면 그 건수를 채우는 즉시 멈추고 재개용 토큰을 돌려준다.
        델타 전체를 한 번에 반환하면 호출측(Cloud Workflows)의 변수 한도 512KB를
        넘겨 실행 자체가 죽고, 토큰이 커밋되지 않아 다음 실행의 델타가 더 커진다 —
        한 번 넘으면 자력으로 못 돌아온다. 그래서 여기서 끊는다.

        끝까지 읽었으면 ``newStartPageToken`` 과 ``False`` 를, 중간에 끊었으면
        ``nextPageToken`` 과 ``True`` 를 돌려준다. 둘 다 다음 ``changes.list`` 의
        ``pageToken`` 으로 그대로 쓸 수 있으므로 호출측은 구분할 필요가 없다.
        """
        changes: list[DriveChange] = []
        token: str | None = page_token
        new_start: str | None = None

        while token:
            resp = (
                self._service.changes()
                .list(
                    pageToken=token,
                    driveId=drive_id,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    spaces="drive",
                    pageSize=100,
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,file(" + FILE_FIELDS + "))"
                    ),
                )
                .execute(num_retries=NUM_RETRIES)
            )
            for item in resp.get("changes", []):
                changes.append(self._to_change(item, drive_id))
            token = resp.get("nextPageToken")
            if "newStartPageToken" in resp:
                new_start = resp["newStartPageToken"]
            if max_changes is not None and token and len(changes) >= max_changes:
                # 재개 지점은 nextPageToken. 이 배치를 커밋하면 다음 호출이 이어받는다.
                return changes, token, True

        if new_start is None:
            # 변경이 없으면 기존 토큰 유지
            new_start = page_token
        return changes, new_start, False

    def download_file(self, file_id: str) -> bytes:
        request = self._service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=NUM_RETRIES)
        return buffer.getvalue()

    def export_file(self, file_id: str, export_mime: str) -> bytes:
        """Google Docs/Sheets/Slides → 바이너리 export."""
        request = self._service.files().export_media(
            fileId=file_id, mimeType=export_mime
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=NUM_RETRIES)
        return buffer.getvalue()

    def get_file(self, file_id: str) -> dict[str, Any]:
        return (
            self._service.files()
            .get(fileId=file_id, supportsAllDrives=True, fields=FILE_FIELDS)
            .execute(num_retries=NUM_RETRIES)
        )

    def get_parents(self, file_id: str) -> list[str]:
        if file_id in self._parent_cache:
            return self._parent_cache[file_id]
        meta = self.get_file(file_id)
        parents = list(meta.get("parents") or [])
        self._parent_cache[file_id] = parents
        name = meta.get("name") or ""
        if name:
            self._name_cache[file_id] = name
        return parents

    def get_name(self, file_id: str) -> str:
        if file_id in self._name_cache:
            return self._name_cache[file_id]
        meta = self.get_file(file_id)
        name = meta.get("name") or ""
        self._name_cache[file_id] = name
        self._parent_cache[file_id] = list(meta.get("parents") or [])
        return name

    def resolve_path_context(
        self,
        file_id: str,
        file_name: str,
        *,
        parents: list[str] | None = None,
        max_depth: int = 32,
    ) -> PathContext:
        """parents를 따라 올라가 폴더명 경로 + 직계 자료묶음(bundle) 계산."""
        folder_names_leaf_to_root: list[str] = []
        current = list(parents) if parents is not None else []
        if parents is None:
            try:
                current = self.get_parents(file_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("path resolve: parents failed %s: %s", file_id, exc)
                current = []

        seen: set[str] = set()
        depth = 0
        while current and depth < max_depth:
            pid = current[0]
            if not pid or pid in seen:
                break
            seen.add(pid)
            try:
                meta = self.get_file(pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("path resolve: folder meta failed %s: %s", pid, exc)
                break
            fname = meta.get("name") or pid
            self._name_cache[pid] = fname
            self._parent_cache[pid] = list(meta.get("parents") or [])
            folder_names_leaf_to_root.append(fname)
            current = list(meta.get("parents") or [])
            depth += 1

        folder_names_leaf_to_root.reverse()
        return build_path_context(folder_names_leaf_to_root, file_name)

    def is_in_sync_scope(
        self,
        file_id: str,
        folder_ids: list[str] | set[str],
        *,
        parents: list[str] | None = None,
    ) -> bool:
        """SYNC_FOLDER_IDS 범위 안이면 True. folder_ids 비면 전체 허용."""
        allow = {f for f in folder_ids if f}
        if not allow:
            return True
        initial = parents
        if initial is None:
            # 조회 실패를 범위 밖(False)으로 바꾸면 일시적 Drive 오류가 문서
            # 삭제로 이어진다. 호출자까지 전파해 배치와 토큰 커밋을 중단한다.
            initial = self.get_parents(file_id)
        return is_under_folder_allowlist(
            file_id=file_id,
            parents=list(initial),
            allowlist=allow,
            resolve_parents=self.get_parents,
        )

    def iter_all_files(self, drive_id: str) -> Iterator[dict[str, Any]]:
        """초기 풀 스캔용 (bootstrap)."""
        page_token: str | None = None
        while True:
            resp = (
                self._service.files()
                .list(
                    driveId=drive_id,
                    corpora="drive",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    q="trashed=false",
                    pageSize=100,
                    pageToken=page_token,
                    fields=f"nextPageToken,files({FILE_FIELDS})",
                )
                .execute(num_retries=NUM_RETRIES)
            )
            for f in resp.get("files", []):
                yield f
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def iter_files_under_folders(self, folder_ids: list[str]) -> Iterator[dict[str, Any]]:
        """지정 폴더 트리만 BFS로 파일 나열 (폴더 자체는 yield 안 함)."""
        folder_mime = "application/vnd.google-apps.folder"
        queue = [fid for fid in folder_ids if fid]
        seen_folders: set[str] = set()
        while queue:
            parent_id = queue.pop()
            if parent_id in seen_folders:
                continue
            seen_folders.add(parent_id)
            page_token: str | None = None
            while True:
                resp = (
                    self._service.files()
                    .list(
                        q=(
                            f"'{parent_id}' in parents and trashed=false"
                        ),
                        corpora="allDrives",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        pageSize=100,
                        pageToken=page_token,
                        fields=f"nextPageToken,files({FILE_FIELDS})",
                    )
                    .execute(num_retries=NUM_RETRIES)
                )
                for f in resp.get("files", []):
                    if f.get("mimeType") == folder_mime:
                        queue.append(f["id"])
                    else:
                        yield f
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

    def iter_backfill_files(
        self, drive_id: str, folder_ids: list[str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """초기 적재용. folder_ids 있으면 그 트리만, 없으면 드라이브 전체."""
        folders = [f for f in (folder_ids or []) if f]
        if folders:
            yield from self.iter_files_under_folders(folders)
            return
        folder_mime = "application/vnd.google-apps.folder"
        for f in self.iter_all_files(drive_id):
            if f.get("mimeType") == folder_mime:
                continue
            yield f

    @staticmethod
    def _to_change(item: dict[str, Any], drive_id: str) -> DriveChange:
        removed = bool(item.get("removed"))
        file_meta = item.get("file") or {}
        file_id = item.get("fileId") or file_meta.get("id") or ""
        trashed = bool(file_meta.get("trashed"))
        return DriveChange(
            file_id=file_id,
            drive_id=file_meta.get("driveId") or drive_id,
            name=file_meta.get("name") or "",
            mime_type=file_meta.get("mimeType") or "",
            modified_time=file_meta.get("modifiedTime"),
            removed=removed or trashed,
            trashed=trashed,
            web_view_link=file_meta.get("webViewLink"),
            md5_checksum=file_meta.get("md5Checksum"),
            size_bytes=parse_drive_size(file_meta.get("size")),
            parents=list(file_meta.get("parents") or []),
        )
