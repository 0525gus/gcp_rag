from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PathContext:
    path: str
    bundle: str
    segments: tuple[str, ...]
    source_title: str = ""
    website_url: str = ""
    metadata_file_id: str = ""

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
    text = markdown.lstrip()
    stripped = False

    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("\n---")
        if end >= 0 and ("path:" in rest[:end] or "bundle:" in rest[:end]):
            text = rest[end + 4 :].lstrip("\n")
            stripped = True

    lines = text.split("\n")
    i = 1 if lines and lines[0].startswith("# ") else 0
    j = i
    while j < len(lines) and (
        not lines[j].strip() or lines[j].startswith(("자료묶음:", "경로:", "원문:", "파일:"))
    ):
        j += 1
    if any(lines[k].startswith(("자료묶음:", "경로:")) for k in range(i, j)):
        return "\n".join(lines[j:]).lstrip("\n")

    return text if stripped else markdown


def build_breadcrumb_markdown(
    *,
    path: str,
    bundle: str,
    title: str,
    body: str = "",
    source_title: str = "",
    website_url: str = "",
) -> str:
    file_title = (title or "").strip() or "untitled"
    title = source_title or file_title
    path = (path or "").strip() or title
    bundle = (bundle or "").strip() or (path.rsplit("/", 1)[0] if "/" in path else "")
    clean_body = strip_breadcrumb(body) if body else ""

    post_header = (f"원문: {website_url}\n\n" if website_url else "")
    if source_title and file_title != source_title:
        post_header += f"파일: {file_title}\n\n"

    if clean_body.strip():
        header = f"# {title}\n\n"
        if bundle:
            header += f"자료묶음: {bundle}\n\n"
        return header + post_header + clean_body.lstrip()

    return (
        f"# {title}\n\n"
        f"자료묶음: {bundle}\n"
        f"경로: {path}\n\n"
        f"{post_header}"
        f"이 파일은 자료묶음 `{bundle}` 소속입니다. "
        "동일 경로의 관련 PDF/PPTX/HWP와 함께 참조하세요.\n"
    )
