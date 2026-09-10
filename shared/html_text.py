"""HTML to readable evidence without scripts, layout attributes or hidden controls.

Uses only the standard library so ingestion and MCP use the same conversion.
Full tables expand row/column spans; incomplete retrieved rows remain readable.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}
_DROP = {"head", "script", "style", "template", "input", "button", "select"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "ul", "ol", "dl",
          "dt", "dd", "blockquote", "pre", "caption"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML permits omitted </td>, </th>, </tr>, </li>, </p> tags.
        if tag in {"td", "th", "tr", "li", "p"}:
            targets = {"td", "th"} if tag in {"td", "th"} else {tag}
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag in targets:
                    del self.stack[i:]
                    break
                if self.stack[i].tag in {"table", "ul", "ol"}:
                    break
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _hidden(node: _Node) -> bool:
    style = re.sub(r"\s+", "", node.attrs.get("style") or "").lower()
    return (node.tag in _DROP or "hidden" in node.attrs
            or (node.attrs.get("aria-hidden") or "").lower() == "true"
            or "display:none" in style or "visibility:hidden" in style
            or "contntMaster" in (node.attrs.get("class") or "").split())


def _span(node: _Node, attr: str) -> int:
    try:
        return max(1, min(100, int(node.attrs.get(attr) or "1")))
    except ValueError:
        return 1


def _table(node: _Node) -> str:
    rows: list[_Node] = []
    captions: list[str] = []

    def collect(parent: _Node) -> None:
        for child in parent.children:
            if not isinstance(child, _Node) or _hidden(child):
                continue
            if child.tag == "tr":
                rows.append(child)
            elif child.tag in {"thead", "tbody", "tfoot"}:
                collect(child)
            elif child.tag == "caption":
                captions.append(_render(child).strip())

    collect(node)
    if not rows:
        return "".join(_render(c) for c in node.children)
    # Repeat a spanned cell so each resulting row retains its classification.
    pending: dict[int, tuple[str, int]] = {}
    lines = []
    notes = []
    for row in rows:
        values = {col: value for col, (value, _) in pending.items()}
        pending = {col: (value, count - 1) for col, (value, count) in pending.items()
                   if count > 1}
        col = 0
        for cell in row.children:
            if not isinstance(cell, _Node) or cell.tag not in {"td", "th"} or _hidden(cell):
                continue
            while col in values:
                col += 1
            value = re.sub(r"\s+", " ", "".join(_render(c) for c in cell.children)).strip()
            value = value.replace("|", "\\|")
            if len(value) > 120 and (_span(cell, "rowspan") > 1 or _span(cell, "colspan") > 1):
                # Long common conditions belong to every spanned row, but repeating
                # the paragraph in each row inflates both indexing and model input.
                label = f"공통셀{len(notes) + 1}"
                notes.append(f"이 표의 {label}: {value}")
                value = f"[{label}]"
            for offset in range(_span(cell, "colspan")):
                values[col + offset] = value
                if _span(cell, "rowspan") > 1:
                    pending[col + offset] = (value, _span(cell, "rowspan") - 1)
            col += _span(cell, "colspan")
        if values:
            lines.append("| " + " | ".join(values.get(i, "") for i in range(max(values) + 1)) + " |")
    return "\n\n" + "\n".join(captions + lines + notes) + "\n\n"


def _render(node: _Node | str) -> str:
    if isinstance(node, str):
        return node
    if _hidden(node):
        return ""
    if node.tag == "table":
        return _table(node)
    if node.tag in {"br", "hr"}:
        return "\n"
    if node.tag == "img":
        return node.attrs.get("alt") or ""
    body = "".join(_render(c) for c in node.children)
    if re.fullmatch(r"h[1-6]", node.tag):
        return "\n\n" + "#" * int(node.tag[1]) + " " + body.strip() + "\n\n"
    if node.tag == "a":
        href = node.attrs.get("href") or ""
        if href.startswith(("https://", "http://", "mailto:")) and body.strip():
            return f"[{body.strip()}]({href})"
    if node.tag == "li":
        return "\n- " + body.strip() + "\n"
    if node.tag in {"td", "th"}:  # partial table chunks without opening <table>
        return body.strip() + " | "
    if node.tag in _BLOCK or node.tag == "tr":
        return "\n" + body + "\n"
    return body


def html_to_text(text: str) -> str:
    parser = _Parser()
    parser.feed(text)
    parser.close()
    result = _render(parser.root).replace("\xa0", " ")
    result = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in result.splitlines())
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def clean_html_evidence(text: str) -> str:
    """Clean legacy indexed HTML only when markup is present (new MD is untouched)."""
    markup = r"</?(?:html|head|body|div|span|table|tr|td|th|p|br|h[1-6])(?:\s|/?>)"
    if re.search(markup, text, re.IGNORECASE):
        return html_to_text(text)
    decoded = html.unescape(text)
    if re.search(markup, decoded, re.IGNORECASE):
        return html_to_text(decoded)
    return text
