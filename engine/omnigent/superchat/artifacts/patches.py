"""Deterministic source patches for a deck: what the editor's controls turn into.

Every patch finds its element by ``data-superchat-id`` in the file's own HTML (stdlib
``html.parser`` gives each tag's exact offset) and rewrites only that element's span, so every
byte outside it is untouched. Anything that cannot be done exactly (a duplicate id, an element
the parser had to close for the author, mixed text and tags) is refused with
:class:`AmbiguousEdit`: the person is told to ask Superchat instead of getting a guess.

Patch kinds (the wire shape, ``kind`` discriminates): ``set-text``, ``set-style``,
``set-attributes`` (``href`` on ``<a>``, ``alt`` on ``<img>``), ``remove-element``,
``duplicate-element``, ``set-theme`` (swap the whole theme, slides untouched) and
``set-full-source`` (undo / redo).

Portions modified from nexu-io/open-design apps/web/src/edit-mode/source-patches.ts@802708f,
Apache-2.0; changes: Python port on ``html.parser`` string spans instead of DOMParser
serialisation (so untouched bytes stay identical), patch kinds trimmed to the set above, and
per-patch human summaries for the "you changed X" note.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from omnigent.superchat.artifact_kinds import MAX_ARTIFACT_BYTES

MAX_PATCHES = 200
MAX_TEXT_CHARS = 20_000
MAX_SUMMARY_CHARS = 400

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROP_RE = re.compile(r"^(?:--[a-z0-9-]{1,60}|[a-z][a-z-]{0,59})$")
_BLOCKED_PROPS = {"behavior", "-moz-binding"}
_BAD_VALUE_RE = re.compile(r"[;{}<>&\\\r\n\x00]|url\(|expression|@import|/\*|\*/", re.I)
_HREF_RE = re.compile(r"^(?:https?://|mailto:|#)[^\s\x00-\x1f]*$", re.I)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"param", "source", "track", "wbr"}
)
_ATTR_RE = re.compile(
    r"""([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""", re.S
)


class PatchError(Exception):
    """Base class: the patches cannot be applied."""


class InvalidPatch(PatchError):
    """The patch itself is malformed (bad value, wrong element kind): a 400."""


class AmbiguousEdit(PatchError):
    """The file cannot be patched exactly here: a 409 whose message says to ask Superchat."""


class SetText(BaseModel):
    kind: Literal["set-text"]
    id: str
    text: str = Field(max_length=MAX_TEXT_CHARS)


class SetStyle(BaseModel):
    kind: Literal["set-style"]
    id: str
    style: dict[str, str | None] = Field(min_length=1, max_length=40)
    #: What the editor showed before (computed values), only to word the summary for a property
    #: the element had no inline declaration for. Never written into the file.
    before: dict[str, str] = Field(default_factory=dict, max_length=40)


class SetAttributes(BaseModel):
    kind: Literal["set-attributes"]
    id: str
    attributes: dict[Literal["href", "alt"], str | None] = Field(min_length=1)


class RemoveElement(BaseModel):
    kind: Literal["remove-element"]
    id: str


class DuplicateElement(BaseModel):
    kind: Literal["duplicate-element"]
    id: str


class SetTheme(BaseModel):
    kind: Literal["set-theme"]
    #: A theme id from the kit's dictionary (``kit.templates()``).
    theme: str = Field(min_length=1, max_length=60)


class SetFullSource(BaseModel):
    kind: Literal["set-full-source"]
    source: str = Field(max_length=MAX_ARTIFACT_BYTES)
    #: What the document editor changed ("edited paragraph 2"); a text document's version note.
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)


Patch = Annotated[
    SetText
    | SetStyle
    | SetAttributes
    | RemoveElement
    | DuplicateElement
    | SetTheme
    | SetFullSource,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------


@dataclass
class _Attr:
    name: str
    value: str  # decoded
    start: int  # span of the whole attribute (name through closing quote)
    end: int
    value_start: int | None  # span of the raw value, quotes included; None for a bare attribute
    value_end: int | None


@dataclass(eq=False)
class _Node:
    tag: str
    start: int
    open_end: int
    parent: _Node | None
    attrs: list[_Attr] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)
    close_start: int | None = None  # start of the end tag (None for void / self-closed)
    end: int = -1
    irregular: bool = False  # the parser had to close it, or it never closed

    def attr(self, name: str) -> _Attr | None:
        return next((a for a in self.attrs if a.name == name), None)

    def classes(self) -> set[str]:
        a = self.attr("class")
        return set(a.value.split()) if a else set()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def _parse_attrs(source: str, node: _Node) -> list[_Attr]:
    raw = source[node.start : node.open_end]
    head = re.match(r"<\s*[^\s/>]+", raw)
    offset = head.end() if head else 1
    body_end = len(raw) - (2 if raw.endswith("/>") else 1)
    attrs: list[_Attr] = []
    for m in _ATTR_RE.finditer(raw, offset, max(body_end, offset)):
        quoted = m.group(2) is not None or m.group(3) is not None
        value = next((g for g in m.groups()[1:] if g is not None), "")
        has_value = m.group(0).find("=") != -1 and (quoted or m.group(4) is not None)
        vs = None
        if has_value:
            # the raw value begins after the "=" and optional blanks
            eq = raw.index("=", m.start(1) + len(m.group(1)))
            vs = eq + 1
            while raw[vs] in " \t\r\n\f":
                vs += 1
        attrs.append(
            _Attr(
                name=m.group(1).lower(),
                value=html.unescape(value),
                start=node.start + m.start(),
                end=node.start + m.end(),
                value_start=None if vs is None else node.start + vs,
                value_end=None if vs is None else node.start + m.end(),
            )
        )
    return attrs


class _Tree(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.root = _Node("#root", 0, 0, None)
        self.stack: list[_Node] = [self.root]
        self._lines = [0]
        for m in re.finditer("\n", source):
            self._lines.append(m.end())

    def _pos(self) -> int:
        line, col = self.getpos()
        return self._lines[line - 1] + col

    def _open(self, tag: str) -> _Node:
        pos = self._pos()
        raw = self.get_starttag_text() or ""
        node = _Node(tag, pos, pos + len(raw), self.stack[-1])
        node.attrs = _parse_attrs(self.source, node)
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):  # noqa: ARG002
        node = self._open(tag)
        if tag in _VOID:
            node.end = node.open_end
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):  # noqa: ARG002
        node = self._open(tag)
        node.end = node.open_end

    def handle_endtag(self, tag):
        pos = self._pos()
        gt = self.source.find(">", pos)
        end = len(self.source) if gt < 0 else gt + 1
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                for skipped in self.stack[depth + 1 :]:
                    skipped.irregular = True
                    skipped.end = pos
                node = self.stack[depth]
                node.close_start, node.end = pos, end
                del self.stack[depth:]
                return
        # a stray end tag: browsers ignore it, and so do we


def _parse(source: str) -> _Node:
    tree = _Tree(source)
    tree.feed(source)
    tree.close()
    for node in tree.stack[1:]:
        node.irregular = True
        node.end = len(source)
    return tree.root


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _clip(text: str, n: int = 50) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _ambiguous(why: str) -> AmbiguousEdit:
    return AmbiguousEdit(f"Can't apply that edit exactly: {why}; ask Superchat instead.")


@dataclass
class _Doc:
    source: str
    root: _Node
    slides: list[_Node]

    @classmethod
    def load(cls, source: str) -> _Doc:
        root = _parse(source)
        slides = [n for n in root.walk() if n.tag == "section" and "slide" in n.classes()]
        return cls(source, root, slides)

    def find(self, element_id: str) -> _Node:
        if not _ID_RE.match(element_id):
            raise InvalidPatch("That element id isn't valid")
        hits = [
            n
            for n in self.root.walk()
            if (a := n.attr("data-superchat-id")) is not None and a.value == element_id
        ]
        if not hits:
            raise _ambiguous(f'no element "{element_id}" in the file (it may have been removed)')
        if len(hits) > 1:
            raise _ambiguous(f'"{element_id}" is used by {len(hits)} elements')
        node = hits[0]
        if any(n.irregular for n in node.walk()) or self._ancestor_irregular(node):
            raise _ambiguous(f'"{element_id}" sits in markup that is not closed cleanly')
        if self.slides and not any(n is node or self._inside(node, n) for n in self.slides):
            raise _ambiguous(f'"{element_id}" is part of the deck frame, not a slide')
        return node

    @staticmethod
    def _inside(node: _Node, ancestor: _Node) -> bool:
        cur = node.parent
        while cur is not None:
            if cur is ancestor:
                return True
            cur = cur.parent
        return False

    def _ancestor_irregular(self, node: _Node) -> bool:
        cur = node.parent
        while cur is not None:
            if cur.irregular:
                return True
            cur = cur.parent
        return False

    def where(self, node: _Node) -> str:
        """``slide 2 title`` for the note."""
        ident = node.attr("data-superchat-id")
        name = ident.value if ident else node.tag
        for i, slide in enumerate(self.slides, 1):
            if slide is node:
                return f"slide {i} ({name})"
            if self._inside(node, slide):
                return f"slide {i} {name}"
        return name

    def ids(self) -> set[str]:
        return {a.value for n in self.root.walk() if (a := n.attr("data-superchat-id"))}


def read_text(source: bytes, element_id: str) -> str | None:
    """The plain text of an element (whitespace collapsed), or ``None`` when the file has none.

    :raises AmbiguousEdit: when the id is used twice or sits in markup that is not closed cleanly.
    """
    doc = _Doc.load(source.decode("utf-8", "replace"))
    if element_id not in doc.ids():
        return None
    node = doc.find(element_id)
    inner = doc.source[node.open_end : node.close_start] if node.close_start is not None else ""
    return " ".join(_plain(re.sub(r"<br\s*/?>", " ", inner)).split())


def _splice(source: str, start: int, end: int, new: str) -> str:
    return source[:start] + new + source[end:]


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;")


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", markup))


# --------------------------------------------------------------------------------------------
# Patch kinds
# --------------------------------------------------------------------------------------------


def _attr_insert_point(doc: _Doc, node: _Node) -> int:
    end = node.open_end - 1
    if doc.source[node.open_end - 2 : node.open_end] == "/>":
        end -= 1
    return end


def _apply_set_text(doc: _Doc, patch: SetText) -> tuple[str, str]:
    node = doc.find(patch.id)
    if node.tag in _VOID or node.tag in {"script", "style", "svg"} or node.close_start is None:
        raise InvalidPatch("That element has no editable text")
    if "\x00" in patch.text:
        raise InvalidPatch("Text can't contain control characters")
    src = doc.source
    target = node
    while True:
        if target.close_start is None:
            raise InvalidPatch("That element has no editable text")
        elems = [c for c in target.children if c.tag != "br"]
        if not elems:
            break
        # a single wrapper (<h1><span>Text</span></h1>) with only whitespace around it
        gaps = _gaps(src, target)
        if len(elems) == 1 and len(target.children) == 1 and not "".join(gaps).strip():
            target = elems[0]
            continue
        raise _ambiguous(
            f'"{patch.id}" mixes text with other styled parts, and a hand edit would flatten them'
        )
    if target.tag in {"script", "style", "svg"}:
        raise InvalidPatch("That element has no editable text")
    inner_start, inner_end = target.open_end, target.close_start
    inner = src[inner_start:inner_end]
    brs = [c for c in target.children if c.tag == "br"]
    br_literal = src[brs[0].start : brs[0].end] if brs else "<br>"
    lines = patch.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 1 and not brs and "\n" not in inner and inner.strip():
        # a one-line element given a line break: keep it a <br>, the common case in decks
        pass
    new_inner = br_literal.join(_escape_text(line) for line in lines)
    # keep the whitespace the author put around the text
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    if not inner.strip():
        lead = trail = ""
    old = _plain(re.sub(r"<br\s*/?>", "\n", inner)).strip()
    new_src = _splice(src, inner_start, inner_end, lead + new_inner + trail)
    return new_src, f'{doc.where(node)} "{_clip(old)}" → "{_clip(patch.text)}"'


def _gaps(src: str, node: _Node) -> list[str]:
    out: list[str] = []
    pos = node.open_end
    for c in node.children:
        out.append(src[pos : c.start])
        pos = c.end
    out.append(src[pos : node.close_start])
    return out


def _split_decls(text: str) -> list[str]:
    out, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == ";" and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [d.strip() for d in out if d.strip()]


def _decl_name(decl: str) -> str:
    return decl.split(":", 1)[0].strip().lower()


def _apply_set_style(doc: _Doc, patch: SetStyle) -> tuple[str, str]:
    node = doc.find(patch.id)
    clean: dict[str, str | None] = {}
    for prop, value in patch.style.items():
        if not _PROP_RE.match(prop) or prop in _BLOCKED_PROPS:
            raise InvalidPatch(f"'{prop}' isn't a style property the editor can set")
        if value is not None:
            value = value.strip().replace('"', "'")
            if not value or len(value) > 300 or _BAD_VALUE_RE.search(value):
                raise InvalidPatch(f"'{value[:40]}' isn't a valid value for {prop}")
        clean[prop] = value
    attr = node.attr("style")
    decls = _split_decls(attr.value) if attr else []
    changes: list[str] = []
    for prop, value in clean.items():
        idx = next((i for i, d in enumerate(decls) if _decl_name(d) == prop), None)
        before = (
            decls[idx].split(":", 1)[1].strip() if idx is not None and ":" in decls[idx] else ""
        ) or " ".join(re.sub(r"[\x00-\x1f<>\"]", " ", patch.before.get(prop, "")).split())[:40]
        if value is None:
            if idx is not None:
                del decls[idx]
        elif idx is not None:
            decls[idx] = f"{prop}: {value}"
        else:
            decls.append(f"{prop}: {value}")
        if (before or "unset") != (value or "none"):
            changes.append(f"{prop} {before or 'unset'}→{value or 'none'}")
    if not changes:
        return doc.source, ""
    css = "; ".join(decls)
    css = f"{css};" if css else ""
    where = doc.where(node)
    src = doc.source
    summary = f"{where} {', '.join(changes)}"
    if attr is None:
        if not css:
            return src, ""
        at = _attr_insert_point(doc, node)
        return _splice(src, at, at, f' style="{_escape_attr(css)}"'), summary
    if not css:
        return _remove_attr(src, attr), summary
    quote = '"'
    new_attr = f"style={quote}{_escape_attr(css)}{quote}"
    return _splice(src, attr.start, attr.end, new_attr), summary


def _remove_attr(src: str, attr: _Attr) -> str:
    start = attr.start
    while start > 0 and src[start - 1] in " \t\r\n":
        start -= 1
    return _splice(src, start, attr.end, "")


def _apply_set_attributes(doc: _Doc, patch: SetAttributes) -> tuple[str, str]:
    node = doc.find(patch.id)
    src = doc.source
    notes: list[str] = []
    # edits go from the back of the tag to the front so earlier spans stay valid
    work: list[tuple[str, str | None]] = list(patch.attributes.items())
    for name, value in work:
        if name == "href" and node.tag != "a":
            raise InvalidPatch("Only a link can have an address")
        if name == "alt" and node.tag != "img":
            raise InvalidPatch("Only an image can have alt text")
        if value is not None:
            value = value.strip()
            if name == "href" and (len(value) > 2000 or not _HREF_RE.match(value)):
                raise InvalidPatch("A link must start with https://, http://, mailto: or #")
            if name == "alt" and (len(value) > 500 or _CTRL_RE.search(value)):
                raise InvalidPatch("That alt text isn't valid")
        node = _Doc.load(src).find(patch.id)
        attr = node.attr(name)
        before = attr.value if attr else ""
        d = _Doc.load(src)
        if value is None:
            if attr is not None:
                src = _remove_attr(src, attr)
        elif attr is not None:
            src = _splice(src, attr.start, attr.end, f'{name}="{_escape_attr(value)}"')
        else:
            at = _attr_insert_point(d, node)
            src = _splice(src, at, at, f' {name}="{_escape_attr(value)}"')
        notes.append(f'{name} "{_clip(before, 40)}" → "{_clip(value or "", 40)}"')
    return src, f"{doc.where(doc.find(patch.id))} {', '.join(notes)}"


def _line_bounds(src: str, start: int, end: int) -> tuple[int, int, bool]:
    """``(line_start, after_newline, alone)``: whether the span is the only thing on its lines."""
    ls = src.rfind("\n", 0, start) + 1
    le = src.find("\n", end)
    le = len(src) if le < 0 else le
    alone = not src[ls:start].strip() and not src[end:le].strip()
    return ls, min(le + 1, len(src)), alone


def _apply_remove(doc: _Doc, patch: RemoveElement) -> tuple[str, str]:
    node = doc.find(patch.id)
    if node in doc.slides and len(doc.slides) == 1:
        raise _ambiguous("a deck needs at least one slide")
    src = doc.source
    ls, after, alone = _line_bounds(src, node.start, node.end)
    note = f"{doc.where(node)}: removed"
    if alone:
        return _splice(src, ls, after, ""), note
    return _splice(src, node.start, node.end, ""), note


def _apply_duplicate(doc: _Doc, patch: DuplicateElement) -> tuple[str, str]:
    node = doc.find(patch.id)
    src = doc.source
    taken = doc.ids()
    pieces: list[tuple[int, int, str]] = []  # spans relative to the copy
    base = node.start
    for n in node.walk():
        a = n.attr("data-superchat-id")
        if a is None or a.value_start is None or a.value_end is None:
            continue
        stem, k = a.value, 1
        while True:
            cand = f"{stem}-copy" if k == 1 else f"{stem}-copy-{k}"
            if cand not in taken:
                break
            k += 1
        taken.add(cand)
        pieces.append((a.value_start - base, a.value_end - base, f'"{cand}"'))
    if node in doc.slides:
        cls = node.attr("class")
        if cls is not None and cls.value_start is not None and cls.value_end is not None:
            kept = " ".join(c for c in cls.value.split() if c != "active")
            pieces.append((cls.value_start - base, cls.value_end - base, f'"{kept}"'))
    copy = src[node.start : node.end]
    for s, e, new in sorted(pieces, reverse=True):
        copy = copy[:s] + new + copy[e:]
    ls, _, alone = _line_bounds(src, node.start, node.end)
    if alone:
        indent = src[ls : node.start]
        new_src = _splice(src, node.end, node.end, f"\n{indent}{copy}")
    else:
        new_src = _splice(src, node.end, node.end, copy)
    return new_src, f"{doc.where(node)}: duplicated"


#: How a ``set-theme`` patch restyles a deck: ``(source, theme id) -> (new source, summary)``.
#: Themes belong to the decks capability, which installs this (``decks/feature.py``); raise
#: ``ValueError`` for an unknown theme and ``AmbiguousEdit`` when the deck can't be restyled.
_theme_restyler: Callable[[str, str], tuple[str, str]] | None = None


def set_theme_restyler(restyle: Callable[[str, str], tuple[str, str]]) -> None:
    """Install how ``set-theme`` restyles a deck (called by the decks capability)."""
    global _theme_restyler
    _theme_restyler = restyle


def _apply_set_theme(doc: _Doc, patch: SetTheme) -> tuple[str, str]:
    if _theme_restyler is None:
        raise InvalidPatch("Themes are not available")
    try:
        return _theme_restyler(doc.source, patch.theme)
    except ValueError as exc:
        raise InvalidPatch(str(exc)) from exc


_RISKY_RE = re.compile(r"<\s*(?:script|iframe|object|embed|form|base)\b", re.I)
_EXTERNAL_RE = re.compile(
    r"""(?:\bsrc|\bhref|\bposter|\bsrcset|\baction)\s*=\s*["']\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import""",
    re.I,
)


def _apply_full_source(doc: _Doc, patch: SetFullSource) -> tuple[str, str]:
    new = patch.source
    if not new.strip():
        raise InvalidPatch("The file can't be empty")
    if len(new.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise InvalidPatch("The file is too large")
    for rx, what in ((_RISKY_RE, "scripts or embeds"), (_EXTERNAL_RE, "web addresses")):
        if len(rx.findall(new)) > len(rx.findall(doc.source)):
            raise InvalidPatch(f"That version adds {what} the file doesn't allow")
    ids = [a.value for n in _parse(new).walk() if (a := n.attr("data-superchat-id"))]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise _ambiguous(f'"{dup[0]}" would be used more than once')
    return new, "restored an earlier state of the file"


# --------------------------------------------------------------------------------------------
# Naming a saved page
# --------------------------------------------------------------------------------------------

# What a person may want to change by hand: text holders by their words, boxes by their class.
_TEXT_TAGS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "a", "button", "label", "blockquote"}
    | {"figcaption", "caption", "summary", "td", "th", "dt", "dd"}
)
_BOX_TAGS = frozenset(
    {"section", "header", "footer", "nav", "main", "article", "aside", "figure", "img", "svg"}
)
_NO_NAMES_INSIDE = frozenset({"head", "script", "style", "svg", "template", "noscript", "select"})


def _slug(text: str, words: int) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "-".join(re.findall(r"[a-z0-9]+", plain)[:words])[:40].strip("-")


def name_elements(source: bytes) -> bytes:
    """Give each element a person may edit by hand a ``data-superchat-id`` when it has none.

    Names come from the element's words (``h3-arches``) or class (``card-arches``), so saving the
    same page again names it the same way. Bytes outside the inserted attributes are untouched;
    a file that isn't UTF-8 comes back as it was.
    """
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return source
    root = _parse(text)
    taken = {a.value for n in root.walk() if (a := n.attr("data-superchat-id"))}
    inserts: list[tuple[int, str]] = []

    def visit(node: _Node) -> None:
        for child in node.children:
            if child.irregular:
                continue
            if child.attr("data-superchat-id") is None:
                base = _base_name(text, child)
                if base:
                    name, n = base, 1
                    while name in taken:
                        n += 1
                        name = f"{base}-{n}"
                    taken.add(name)
                    end = child.open_end - (
                        2 if text[child.open_end - 2 : child.open_end] == "/>" else 1
                    )
                    inserts.append((end, f' data-superchat-id="{name}"'))
            if child.tag not in _NO_NAMES_INSIDE:
                visit(child)

    visit(root)
    for at, attr in reversed(inserts):
        text = text[:at] + attr + text[at:]
    return text.encode("utf-8") if inserts else source


def _base_name(source: str, node: _Node) -> str | None:
    inner = source[node.open_end : node.close_start] if node.close_start else ""
    words = html.unescape(re.sub(r"<[^>]*>", " ", inner))
    if node.tag in _TEXT_TAGS:
        if not words.strip():
            return None
        return f"{node.tag}-{_slug(words, 4)}".rstrip("-")
    if node.tag in _BOX_TAGS or (node.tag == "div" and node.classes()):
        kind = _slug(sorted(node.classes())[0], 3) if node.classes() else node.tag
        if node.tag == "img":
            alt = node.attr("alt")
            words = alt.value if alt else ""
        return "-".join(p for p in (kind or node.tag, _slug(words, 2)) if p)
    return None


_APPLY = {
    "set-text": _apply_set_text,
    "set-style": _apply_set_style,
    "set-attributes": _apply_set_attributes,
    "remove-element": _apply_remove,
    "duplicate-element": _apply_duplicate,
    "set-theme": _apply_set_theme,
    "set-full-source": _apply_full_source,
}


def changed_ids(patches: list[Patch]) -> tuple[str, ...]:
    """The element ids *patches* change, in order, once each: a removed element is gone and a
    theme or a whole-source edit names none."""
    ids: list[str] = []
    for patch in patches:
        if isinstance(patch, (SetText, SetStyle, SetAttributes, DuplicateElement)):
            if patch.id not in ids:
                ids.append(patch.id)
    return tuple(ids)


def apply_patches(
    source: bytes,
    patches: list[
        SetText
        | SetStyle
        | SetAttributes
        | RemoveElement
        | DuplicateElement
        | SetTheme
        | SetFullSource
    ],
) -> tuple[bytes, str]:
    """Apply ``patches`` in order; return ``(new_bytes, summary)``.

    :raises InvalidPatch: for a malformed patch (HTTP 400).
    :raises AmbiguousEdit: when it cannot be done exactly (HTTP 409, "ask Superchat instead").
    """
    if not patches or len(patches) > MAX_PATCHES:
        raise InvalidPatch(f"Send between 1 and {MAX_PATCHES} patches")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidPatch("The file isn't UTF-8 text") from exc
    notes: list[str] = []
    for patch in patches:
        doc = _Doc.load(text)
        if patch.kind not in ("set-full-source", "set-theme"):
            target = doc.find(patch.id)
            ident = target.attr("data-superchat-id")
            if ident is not None and ident.value_start is None:
                raise _ambiguous("that element has no readable id")
        text, note = _APPLY[patch.kind](doc, patch)  # type: ignore[operator]
        if note:
            notes.append(note)
    out = text.encode("utf-8")
    if len(out) > MAX_ARTIFACT_BYTES:
        raise InvalidPatch("The file is too large")
    summary = "; ".join(notes)
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return out, summary


# --------------------------------------------------------------------------------------------
# A hand edit in plain words (the studio thread, ADR 0024)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EditNote:
    """One hand edit as the studio thread shows it, built by code from a version's summary.

    :param element_id: The edited element's ``data-superchat-id``; ``None`` when the edit is not
        about one element (a theme switch, an undo, a document edit, several elements).
    :param element: The element in words ("the title", "slide 2"); ``""`` without one.
    :param what: The sentence a person reads ("You made the title larger"), never the CSS.
    """

    element_id: str | None
    element: str
    what: str


#: One note of :func:`apply_patches`' summary: where (``slide 2 title``, ``slide 2 (cover)``,
#: ``title``) and what (``font-size 44px→52px``, ``"A" → "B"``, ``: removed``).
_NOTE_RE = re.compile(
    r"^(?:slide (?P<slide>\d+) )?(?:\((?P<own>[^()\s]+)\)|(?P<id>[^\s:]+))(?P<rest>.*)$"
)
_STYLE_CHANGE_RE = re.compile(r"(?P<prop>[a-z-]+) (?P<before>[^→]*)→(?P<after>[^,]*)(?:, |$)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_SIZE_PROPS = frozenset({"font-size", "width", "height", "scale", "zoom"})
_COLOUR_PROPS = frozenset(
    {"color", "background", "background-color", "border-color", "fill", "stroke"}
)
_POSITION_PROPS = frozenset(
    {"left", "top", "right", "bottom", "inset", "transform", "translate"}
    | {"margin", "margin-top", "margin-left", "margin-right", "margin-bottom"}
)
#: Words in an element id that name what it is, the last one winning (``cover-title``).
_ELEMENT_WORDS = frozenset(
    {"title", "subtitle", "heading", "headline", "kicker", "lead", "chart", "image", "logo"}
    | {"photo", "button", "card", "table", "caption", "quote", "footer", "header", "list"}
    | {"note", "label", "link", "icon", "stat", "metric", "number", "legend", "bar"}
)
#: The element's tag (the first word of a name ``name_elements`` gave it) in words.
_TAG_WORDS = {
    "h1": "title",
    "h2": "title",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "p": "paragraph",
    "img": "image",
    "a": "link",
    "li": "list item",
    "ul": "list",
    "ol": "list",
    "button": "button",
    "table": "table",
    "figure": "figure",
    "blockquote": "quote",
}


def _element_words(element_id: str) -> str:
    words = [w for w in re.split(r"[-_.:]+", element_id.lower()) if w]
    named = [w for w in words if w in _ELEMENT_WORDS]
    if named:
        return f"the {named[-1]}"
    if words and words[0] in _TAG_WORDS:
        return f"the {_TAG_WORDS[words[0]]}"
    return "the " + " ".join(w for w in words if not w.isdigit()) if words else "the element"


def _style_what(rest: str, element: str) -> str:
    changes = list(_STYLE_CHANGE_RE.finditer(rest.strip()))
    props = {m["prop"] for m in changes}
    if not props:
        return f"You changed {element}"
    if props <= _SIZE_PROPS:
        before = sum(float(n) for m in changes for n in _NUMBER_RE.findall(m["before"])[:1])
        after = sum(float(n) for m in changes for n in _NUMBER_RE.findall(m["after"])[:1])
        if after > before:
            return f"You made {element} larger"
        if after < before:
            return f"You made {element} smaller"
        return f"You resized {element}"
    if props <= _COLOUR_PROPS:
        return f"You changed the colour of {element}"
    if props <= _POSITION_PROPS:
        return f"You moved {element}"
    return f"You changed {element}"


def _note_of(note: str) -> tuple[str, str, str] | None:
    """``(element_id, element, what)`` for one summary note about an element, else ``None``."""
    match = _NOTE_RE.match(note)
    if not match:
        return None
    rest = match["rest"]
    if match["own"] is not None:  # the slide itself
        if match["slide"] is None:
            return None
        element_id, element = match["own"], f"slide {match['slide']}"
    else:
        element_id = match["id"]
        element = _element_words(element_id)
    if rest == ": removed":
        return element_id, element, f"You removed {element}"
    if rest == ": duplicated":
        return element_id, element, f"You duplicated {element}"
    if rest.startswith(' "') and '" → "' in rest:
        return element_id, element, f"You changed the text of {element}"
    if rest.startswith((" href ", " alt ")):
        return element_id, element, f"You changed {element}"
    if _STYLE_CHANGE_RE.search(rest):
        return element_id, element, _style_what(rest, element)
    return None


def edit_note(summary: str | None) -> EditNote:
    """A hand edit's :class:`EditNote` from the summary :func:`apply_patches` wrote for it.

    The summary is this module's own format (one note per patch, ``"; "`` between them), so the
    reading lives beside the writing. An edit to several elements, or one that is not about an
    element (a theme switch, an undo, a document's own summary), reads "You " + its summary.
    """
    text = " ".join((summary or "").split())
    notes = [_note_of(part) for part in text.split("; ")] if text else []
    found = [note for note in notes if note is not None]
    if found and len(found) == len(notes) and len({n[0] for n in found}) == 1:
        element_id, element, what = found[0]
        if len(found) > 1:
            what = f"You changed {element}"
        return EditNote(element_id, element, what)
    if found:
        return EditNote(None, "", "You made several changes")
    return EditNote(None, "", f"You {text}" if text else "You edited this file")
