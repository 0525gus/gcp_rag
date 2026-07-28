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
    """재파싱 시 중복 헤더 방지.

    구형(YAML frontmatter)과 신형(`# 제목` + `자료묶음:`) 둘 다 걷어낸다.
    GCS 에 이미 올라간 산출물이 구형이라, 신형만 알아보면 재파싱 때 헤더가
    두 겹으로 쌓인다.
    """
    text = markdown.lstrip()
    stripped = False

    # 구형 YAML frontmatter
    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("\n---")
        if end >= 0 and ("path:" in rest[:end] or "bundle:" in rest[:end]):
            text = rest[end + 4 :].lstrip("\n")
            stripped = True

    # 신·구형 공통: `# 제목` 다음에 오는 `자료묶음:` / `경로:` 줄까지
    lines = text.split("\n")
    i = 1 if lines and lines[0].startswith("# ") else 0
    j = i
    while j < len(lines) and (
        not lines[j].strip() or lines[j].startswith(("자료묶음:", "경로:"))
    ):
        j += 1
    # 메타 줄을 실제로 하나라도 먹었을 때만 헤더로 인정한다.
    # 아니면 본문이 그냥 `# 제목` 으로 시작한 것이라 건드리면 안 된다.
    if any(lines[k].startswith(("자료묶음:", "경로:")) for k in range(i, j)):
        return "\n".join(lines[j:]).lstrip("\n")

    return text if stripped else markdown


def build_breadcrumb_markdown(
    *,
    path: str,
    bundle: str,
    title: str,
    body: str = "",
) -> str:
    """인덱스용 텍스트. 본문이 있으면 앞에 붙이고, 없으면 sidecar용 짧은 문서.

    머리말은 **제목과 자료묶음을 한 번씩만** 싣는다. 예전에는 YAML
    frontmatter(path/bundle/title) + `# 제목` + `자료묶음:` + `경로:` 를 모두
    실어서 같은 제목이 문서마다 6번 반복됐다. 실측(무작위 50건): 머리말
    중앙값 308자, 첫 청크(≈980자)의 31%. 본문 중앙값이 828자라 대부분
    문서가 청크 하나인데, 그 청크의 3분의 1이 코퍼스 전체가 공유하는
    같은 문장이었다. 문서끼리 임베딩이 서로 가까워져 공문·인사발령·규정
    개정안처럼 서식이 같은 무리에서 구별이 안 됐다.

    path 는 통째로 뺀다. 앞부분(`Drive/문서결재/2026_문서접수/`)은 거의 모든
    문서가 공유해 신호가 없고, 잎 폴더는 bundle 과 같으며, 파일명은 title
    과 같다. 검색 결과의 경로 표시는 Firestore 메타에서 오므로 영향 없다.

    다만 **sidecar(본문 없는 xlsx 등)는 예전 그대로** 둔다. 그쪽은 머리말이
    유일한 내용이라 줄이면 찾을 단서 자체가 사라진다.
    """
    title = (title or "").strip() or "untitled"
    path = (path or "").strip() or title
    bundle = (bundle or "").strip() or (path.rsplit("/", 1)[0] if "/" in path else "")
    clean_body = strip_breadcrumb(body) if body else ""

    if clean_body.strip():
        header = f"# {title}\n\n"
        if bundle:
            header += f"자료묶음: {bundle}\n\n"
        return header + clean_body.lstrip()

    return (
        f"# {title}\n\n"
        f"자료묶음: {bundle}\n"
        f"경로: {path}\n\n"
        f"이 파일은 자료묶음 `{bundle}` 소속입니다. "
        "동일 경로의 관련 PDF/PPTX/HWP와 함께 참조하세요.\n"
    )
