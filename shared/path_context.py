"""Drive 폴더 경로 → RAG용 breadcrumb / 자료묶음(bundle) 메타."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PathContext:
    """예: 컴공/문서결재/2026 digital training/안내.pdf → bundle=2026 digital training."""

    path: str
    bundle: str
    segments: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return bool(self.path)


def build_path_context(
    folder_names_root_to_parent: list[str],
    file_name: str,
) -> PathContext:
    name = (file_name or "").strip() or "untitled"
    folders = [f.strip() for f in folder_names_root_to_parent if f and f.strip()]
    bundle = folders[-1] if folders else ""
    segments = tuple(folders + [name])
    return PathContext(path="/".join(segments), bundle=bundle, segments=segments)


def strip_breadcrumb(markdown: str) -> str:
    """재파싱 시 중복 헤더 방지."""
    text = markdown.lstrip()
    if not text.startswith("---"):
        return markdown
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        return markdown
    after = rest[end + 4 :].lstrip("\n")
    # 우리가 넣은 키만 있을 때 제거
    front = rest[:end]
    if "path:" in front or "bundle:" in front:
        return after
    return markdown


def build_breadcrumb_markdown(
    *,
    path: str,
    bundle: str,
    title: str,
    body: str = "",
) -> str:
    """인덱스용 텍스트. 본문이 있으면 앞에 붙이고, 없으면 sidecar용 짧은 문서."""
    title = (title or "").strip() or "untitled"
    path = (path or "").strip() or title
    bundle = (bundle or "").strip() or (path.rsplit("/", 1)[0] if "/" in path else "")
    clean_body = strip_breadcrumb(body) if body else ""

    header = (
        "---\n"
        f"path: {path}\n"
        f"bundle: {bundle}\n"
        f"title: {title}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"자료묶음: {bundle}\n"
        f"경로: {path}\n\n"
    )
    if clean_body.strip():
        return header + clean_body.lstrip()
    return (
        header
        + f"이 파일은 자료묶음 `{bundle}` 소속입니다. "
        + "동일 경로의 관련 PDF/PPTX/HWP와 함께 참조하세요.\n"
    )
