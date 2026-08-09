"""A very small HTML tree + selector engine.

Adapters parse saved fixtures in CI with no network and no heavyweight parser
dependency (§19.2). If ``selectolax`` is installed it is not required; this
module is intentionally dependency-free so fixture tests always run.

Supported selector syntax (a deliberate subset of CSS):

``tag``, ``.class``, ``#id``, ``tag.class``, ``[attr]``, ``[attr=value]``,
``[attr*=value]``, and descendant combinations separated by spaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

TEXT_NODE = "#text"

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    parent: Node | None = None

    # -- traversal --------------------------------------------------------
    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self, separator: str = " ") -> str:
        """Text in document order, skipping script/style subtrees."""

        chunks: list[str] = []

        def visit(node: Node) -> None:
            if node.tag in {"script", "style"}:
                return
            chunks.extend(part for part in node.text_parts if part.strip())
            for child in node.children:
                visit(child)

        visit(self)
        return re.sub(r"\s+", " ", separator.join(chunks)).strip()

    def raw_text(self) -> str:
        return "".join(part for node in self.walk() for part in node.text_parts)

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    # -- selection --------------------------------------------------------
    def select(self, selector: str) -> list[Node]:
        return select(self, selector)

    def select_one(self, selector: str) -> Node | None:
        matches = self.select(selector)
        return matches[0] if matches else None

    def first_text(self, *selectors: str) -> str:
        """Text of the first selector that matches anything (selector fallback chain)."""

        for selector in selectors:
            node = self.select_one(selector)
            if node is not None:
                value = node.text()
                if value:
                    return value
        return ""

    def first_attr(self, selector: str, attribute: str) -> str:
        node = self.select_one(selector)
        return node.attr(attribute) if node else ""


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag=tag,
            attrs={key: (value or "") for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS and self._stack[-1].tag == tag:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        # Text is a child node so that mixed content keeps document order:
        # "<li><b>3</b> bds</li>" must read "3 bds", never "bds 3".
        parent = self._stack[-1]
        parent.children.append(Node(tag=TEXT_NODE, text_parts=[data], parent=parent))


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


_SIMPLE = re.compile(
    r"^(?P<tag>[a-zA-Z0-9_-]+|\*)?"
    r"(?P<id>#[\w-]+)?"
    r"(?P<classes>(?:\.[\w-]+)*)"
    r"(?P<attrs>(?:\[[^\]]+\])*)$"
)
_ATTR = re.compile(r"\[([\w:-]+)(?:([~*^$]?=)\"?'?([^\]\"']*)\"?'?)?\]")


def _matches_simple(node: Node, selector: str) -> bool:
    if node.tag == TEXT_NODE:
        return False
    match = _SIMPLE.match(selector)
    if not match:
        return False
    tag = match.group("tag")
    if tag and tag != "*" and node.tag != tag:
        return False
    node_id = match.group("id")
    if node_id and node.attr("id") != node_id[1:]:
        return False
    classes = match.group("classes")
    if classes:
        required = {name for name in classes.split(".") if name}
        if not required.issubset(node.classes):
            return False
    attrs = match.group("attrs")
    if attrs:
        for name, operator, value in _ATTR.findall(attrs):
            if name not in node.attrs:
                return False
            if operator:
                actual = node.attrs[name]
                if operator == "=" and actual != value:
                    return False
                if operator == "*=" and value not in actual:
                    return False
                if operator == "^=" and not actual.startswith(value):
                    return False
                if operator == "$=" and not actual.endswith(value):
                    return False
                if operator == "~=" and value not in actual.split():
                    return False
    return True


def select(root: Node, selector: str) -> list[Node]:
    """Select descendants of ``root`` matching a whitespace-separated selector."""

    parts = [part for part in selector.strip().split() if part]
    if not parts:
        return []
    current = [root]
    for part in parts:
        matched: list[Node] = []
        seen: set[int] = set()
        for scope in current:
            for node in scope.walk():
                if node is scope:
                    continue
                if _matches_simple(node, part) and id(node) not in seen:
                    matched.append(node)
                    seen.add(id(node))
        current = matched
        if not current:
            return []
    return current


_SCRIPT_JSON = re.compile(
    r"<script[^>]*id=[\"'](?P<id>[^\"']+)[\"'][^>]*>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def embedded_json_blocks(html: str) -> dict[str, str]:
    """Return ``{script_id: raw_json_text}`` for id-bearing script tags.

    Modern listing sites hydrate from a JSON blob; reading it is both more
    stable and gentler than scraping the rendered DOM.
    """

    blocks: dict[str, str] = {}
    for match in _SCRIPT_JSON.finditer(html):
        body = unescape(match.group("body")).strip()
        if body.startswith("{") or body.startswith("["):
            blocks[match.group("id")] = body
    return blocks


def text_of(html: str) -> str:
    return parse_html(html).text()
