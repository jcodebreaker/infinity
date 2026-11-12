"""The deck kit: assembles a Superchat deck file from the skeleton, a template and the slides.

A deck is one self-contained HTML file (``*.deck.html``): the fixed 1920x1080 skeleton (scale to
fit, navigation, print rules, the Superchat Deck Protocol), a template's tokens, theme and
Latin-subset fonts (embedded as data URIs, because the viewer's CSP blocks remote fonts), the
shared layout vocabulary, and the slides the Muse wrote. Only the slides are the Muse's; everything
else comes from here, so every deck starts exact for the PowerPoint export.

Portions modified from nexu-io/open-design
packages/contracts/src/prompts/deck-framework.ts@802708f, Apache-2.0; changes: the skeleton is
``kit/skeleton.html`` (see its header), slides are inserted by this module instead of being
hand-copied by the model, and the template set, tokens and fonts are Superchat's
(``kit/templates``, MIT upstream designs; fonts SIL OFL 1.1).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

KIT_DIR = Path(__file__).parent / "kit"
TEMPLATES_DIR = KIT_DIR / "templates"
FONTS_DIR = KIT_DIR / "fonts"
CHARTS_DIR = KIT_DIR / "charts"
CHARTJS_PATH = KIT_DIR / "vendor" / "chart.js" / "chart.umd.js"

#: The restrained professional theme the Muse starts from unless the request calls for more.
DEFAULT_THEME = "corporate-clean"
PREVIEWS_DIR = KIT_DIR / "previews"
SLOTS_OPEN = "<!-- superchat:slides -->"
SLOTS_CLOSE = "<!-- /superchat:slides -->"
_FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".woff2": "font/woff2"}


class KitError(ValueError):
    """The request cannot be turned into a deck (unknown template, bad slides, ...)."""


@dataclass(frozen=True)
class Template:
    """One deck template: a design (palette, type, motifs) over the shared layouts."""

    id: str
    name: str
    tagline: str
    description: str
    best_for: str
    category: str
    mode: str
    fonts: tuple[dict[str, object], ...]

    @property
    def directory(self) -> Path:
        return TEMPLATES_DIR / self.id


@lru_cache(maxsize=1)
def templates() -> dict[str, Template]:
    """Every template under ``kit/templates``, by id."""
    found: dict[str, Template] = {}
    for meta in sorted(TEMPLATES_DIR.glob("*/template.json")):
        data = json.loads(meta.read_text("utf-8"))
        found[data["id"]] = Template(
            id=data["id"],
            name=data["name"],
            tagline=data["tagline"],
            description=data["description"],
            best_for=data["best_for"],
            category=data["category"],
            mode=data["mode"],
            fonts=tuple(data["fonts"]),
        )
    return found


def theme_dictionary() -> list[dict[str, str]]:
    """The theme dictionary: ``[{id, name, tagline, mood, category, mode, best_for}]``.

    ``mood`` is the theme's one-line description and ``tagline`` its 2-4 word gist (what a picker
    tile shows). The order is stable, restrained first: professional themes, then editorial, bold
    and dark, each by id.
    """
    order = {"professional": 0, "editorial": 1, "bold": 2, "dark": 3}
    ranked = sorted(templates().values(), key=lambda t: (order.get(t.category, 9), t.id))
    return [
        {
            "id": t.id,
            "name": t.name,
            "tagline": t.tagline,
            "mood": t.description,
            "category": t.category,
            "mode": t.mode,
            "best_for": t.best_for,
        }
        for t in ranked
    ]


@cache
def theme_gallery() -> list[dict[str, str]]:
    """The dictionary for the picker: each entry plus its thumbnail as a ``data:`` URI."""
    out = []
    for entry in theme_dictionary():
        path = PREVIEWS_DIR / f"{entry['id']}.webp"
        data = base64.b64encode(path.read_bytes()).decode("ascii") if path.is_file() else ""
        out.append({**entry, "preview": f"data:image/webp;base64,{data}" if data else ""})
    return out


#: The sample slides a theme preview shows (``data-superchat-id`` of each, in order): the cover, an
#: agenda, figures, a chart, a table, a quote and the close. All of it is made-up sample content.
PREVIEW_SLIDES = ("cover", "agenda", "numbers", "trend", "teams", "voice", "decision")
_SECTION_RE = re.compile(
    r'\s*<section\b[^>]*\bdata-superchat-id="([a-z0-9-]+)".*?</section>', re.S
)


@cache
def sample_deck(template_id: str) -> str:
    """The kit's sample deck in ``template_id``, cut to :data:`PREVIEW_SLIDES`, for a preview."""
    sample = (KIT_DIR / "sample-slides.html").read_text("utf-8")
    by_id = {m.group(1): m.group(0) for m in _SECTION_RE.finditer(sample)}
    slides = []
    for n, slide_id in enumerate(PREVIEW_SLIDES, 1):
        slide = re.sub(r'data-screen-label="\d+ ', f'data-screen-label="{n:02d} ', by_id[slide_id])
        slides.append(re.sub(r'(-foot-page">)\d+<', rf"\g<1>{n:02d}<", slide))
    return build_deck(template_id, "Quarterly review", "".join(slides))


@cache
def _font_faces_css(template_id: str) -> str:
    """The template's ``@font-face`` rules with the font files inlined as data URIs."""
    template = templates()[template_id]
    rules = []
    for font in template.fonts:
        path = FONTS_DIR / str(font["file"])
        mime = _FONT_MIME.get(path.suffix.lower(), "font/ttf")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f'@font-face{{font-family:"{font["family"]}";font-style:{font["style"]};'
            f"font-weight:{font['weight']};font-display:block;"
            f'src:url(data:{mime};base64,{data}) format("truetype")}}'
        )
    return "\n".join(rules)


def chart_recipes() -> dict[str, str]:
    """The chart slide recipes by name (``chart-bar`` ...): Open Design's templates, ported."""
    return {path.stem: path.read_text("utf-8") for path in sorted(CHARTS_DIR.glob("chart-*.html"))}


_LAYOUT_RE = re.compile(r'<section\b[^>]*\bclass="[^"]*\b(l-[a-z]+)\b[^"]*"')


def layout_names() -> list[str]:
    """What ``deck_new(layout=…)`` gives an example of: each layout (``l-cover`` ...) of the
    sample deck, then each chart recipe (``chart-bar`` ...)."""
    sample = (KIT_DIR / "sample-slides.html").read_text("utf-8")
    layouts = list(dict.fromkeys(_LAYOUT_RE.findall(sample)))
    return [*layouts, *chart_recipes()]


def layout_example(name: str) -> str | None:
    """One example slide (the sample deck's first of that layout) or one chart recipe, or
    ``None`` for a name that is neither. The layout menu itself is the deck skill (the bundle's
    ``skills/decks/SKILL.md``, rendered from ``kit/menu.md``), read on demand."""
    recipes = chart_recipes()
    if name in recipes:
        return recipes[name].strip()
    sample = (KIT_DIR / "sample-slides.html").read_text("utf-8")
    for match in _SECTION_RE.finditer(sample):
        found = _LAYOUT_RE.match(match.group(0).strip())
        if found is not None and found.group(1) == name:
            return match.group(0).strip()
    return None


@cache
def _chart_runtime() -> str:
    """Chart.js (vendored, MIT) and Superchat's chart defaults: the deck's inline head script."""
    library = CHARTJS_PATH.read_text("utf-8")
    bootstrap = (CHARTS_DIR / "bootstrap.js").read_text("utf-8")
    return (
        "  <script data-superchat-chartjs>\n"
        "/* Chart.js (MIT), vendored: never read or edit this block. */\n"
        f"{library}\n"
        "  </script>\n"
        f"  <script data-superchat-chartjs-defaults>\n{bootstrap}  </script>\n"
    )


# ---------------------------------------------------------------------------------------------
# Static checks on the slides (before and after assembly); the layout checks run in Chromium.
# ---------------------------------------------------------------------------------------------

_SLIDE_RE = re.compile(r"<section\b[^>]*\bclass\s*=\s*\"[^\"]*\bslide\b[^\"]*\"[^>]*>", re.I)
_ID_RE = re.compile(r"""\bdata-superchat-id\s*=\s*["']([^"']*)["']""")
_EXTERNAL_RE = re.compile(
    r"""(?:\bsrc|\bhref|\bposter|\bsrcset|\baction)\s*=\s*["']\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import""",
    re.I,
)
# A link's href opens a page on a click and loads nothing, so it is left out of the check above;
# an SVG <image>/<use> href, and any url() in the link's own style, still count.
_LINK_HREF_RE = re.compile(
    r"""(<a\b[^>]*?)\s(?:xlink:)?href\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I
)
_FORBIDDEN_TAG_RE = re.compile(r"<\s*(script|iframe|object|embed|link|base|form|meta)\b", re.I)
# The one script a slide may carry: the Chart.js call that draws a chart on its canvas.
_CHART_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\bdata-superchat-chart(?:\s|=|>))[^>]*>(.*?)</script\s*>", re.I | re.S
)
_ID_FORMAT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def check_slides(slides_html: str) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the slides markup: structure, ids, nothing external."""
    errors: list[str] = []
    warnings: list[str] = []
    tags = _SLIDE_RE.findall(slides_html)
    if not tags:
        errors.append('No slides: write each slide as <section class="slide ..."> ... </section>.')
    if slides_html.lower().count("<section") != slides_html.lower().count("</section>"):
        errors.append("A <section> is not closed.")
    for tag in tags:
        if "data-screen-label" not in tag:
            errors.append('A slide has no data-screen-label (for example "02 Problem").')
        if "data-superchat-id" not in tag:
            errors.append("A slide has no data-superchat-id.")
    plain = _CHART_SCRIPT_RE.sub("", slides_html)
    if _FORBIDDEN_TAG_RE.search(plain):
        errors.append(
            "Slides may not contain <script>, <iframe>, <object>, <embed>, <link>, <base>, "
            "<form> or <meta> (the one exception is <script data-superchat-chart>, a chart's "
            "data)."
        )
    for script in _CHART_SCRIPT_RE.findall(slides_html):
        if "new Chart(" not in script:
            errors.append(
                "A <script data-superchat-chart> must create a Chart.js chart with new Chart(...)."
            )
        if re.search(
            r"\b(fetch|XMLHttpRequest|import|eval|Function|WebSocket)\s*\(|\bdocument\.write",
            script,
        ):
            errors.append(
                "A <script data-superchat-chart> may only configure its chart: the data goes in "
                "the script, nothing is fetched or evaluated."
            )
    loading = _LINK_HREF_RE.sub(r"\1", slides_html)
    external = _EXTERNAL_RE.search(loading)
    if external:
        near = loading[external.start() : external.end() + 60].split(">", 1)[0]
        errors.append(
            "Slides may not load anything from the web (images, fonts, @import), as in "
            f"`{near}`: remove every such reference, drawing it with HTML/CSS/SVG or leaving "
            "it out."
        )
    ids = _ID_RE.findall(slides_html)
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f'data-superchat-id "{dup}" is used more than once.')
    for bad in sorted({i for i in ids if not _ID_FORMAT.match(i)}):
        errors.append(f'data-superchat-id "{bad}" must be lower-case words joined by hyphens.')
    if tags and "active" not in tags[0].split("class=", 1)[-1].split('"')[1].split():
        warnings.append('The first slide should have class="slide active ...".')
    return errors, warnings


# A built deck separates what the kit owns from what is the deck's own. The kit owns two marked
# regions (the theme's tokens inside the framework's ``:root``, and the layout vocabulary plus
# theme CSS in the first per-deck ``<style>``) and the embedded-font block. Everything outside
# the markers is the deck's: custom rules, custom tokens and overrides of a theme token (a later
# ``:root { --accent: ... }`` after the markers). A theme switch rewrites only the kit's regions
# and carries the rest over byte for byte, so a token override persists exactly because someone
# wrote it outside the kit's regions: the kit never writes there.
#
# Each open marker carries a hash of the region's content (``sha=``). A region that still matches
# its hash is the kit's and is replaced whole, whatever kit version wrote it. One that does not
# match was edited inside, and the switch is refused: nothing is dropped and the deck is not
# changed (per-deck CSS belongs after the close marker).
_TOKENS_OPEN = "      /* superchat:theme-tokens id={id} sha={sha} */"
_TOKENS_CLOSE = "      /* /superchat:theme-tokens */"
_CSS_OPEN = "    /* superchat:theme id={id} sha={sha} \u00b7 {name} */"
_CSS_CLOSE = "    /* /superchat:theme */"
_DECK_HINT = (
    "    /* Per-deck rules and token overrides (for example :root { --accent: ... }) go below,\n"
    "       after the closing superchat:theme marker; a theme switch keeps everything outside"
    " the\n"
    "       markers. Do not edit between them. */"
)
_ROOT_RE = re.compile(r"    :root \{.*?\n    \}", re.S)
_TOKENS_RE = re.compile(
    r"      /\* superchat:theme-tokens id=([a-z0-9-]+)(?: sha=([0-9a-f]{12}))? \*/\n"
    r"(.*?)\n      /\* /superchat:theme-tokens \*/",
    re.S,
)
_CSS_RE = re.compile(
    r"    /\* superchat:theme id=([a-z0-9-]+)(?: sha=([0-9a-f]{12}))? \u00b7 [^\n]*?\*/\n"
    r"(.*?)\n    /\* /superchat:theme \*/",
    re.S,
)
_THEME_ID_RE = re.compile(r"/\* superchat:theme id=([a-z0-9-]+)[ *]")
# Decks saved before the markers existed: one whole ``:root`` and one whole style block.
_LEGACY_STYLE_RE = re.compile(
    r"  <style>\n    /\* [^\n]*? \u00b7 layout vocabulary and theme \*/.*?\n  </style>", re.S
)
_LEGACY_NAME_RE = re.compile(r"/\* ([^\n]*?) \u00b7 layout vocabulary and theme \*/")
_PLACEHOLDER_STYLE_RE = re.compile(
    r"  <style>\n    /\* SLOT: per-deck styles.*?\n  </style>", re.S
)
_FONTS_RE = re.compile(r"  <style data-superchat-fonts>.*?\n  </style>\n", re.S)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _tokens_text(template: Template) -> str:
    return (template.directory / "tokens.css").read_text("utf-8").rstrip()


def _css_text(template: Template) -> str:
    theme = (template.directory / "theme.css").read_text("utf-8").strip()
    layouts = (KIT_DIR / "layouts.css").read_text("utf-8").strip()
    return f"{layouts}\n\n{theme}"


def _tokens_region(template: Template) -> str:
    text = _tokens_text(template)
    open_ = _TOKENS_OPEN.format(id=template.id, sha=_digest(text))
    return f"{open_}\n{text}\n{_TOKENS_CLOSE}"


def _css_region(template: Template) -> str:
    text = _css_text(template)
    open_ = _CSS_OPEN.format(id=template.id, sha=_digest(text), name=template.name)
    return f"{open_}\n{text}\n{_CSS_CLOSE}"


def _tokens_block(template: Template) -> str:
    return "    :root {\n" + _tokens_region(template) + "\n    }"


def _style_block(template: Template) -> str:
    return f"  <style>\n{_css_region(template)}\n{_DECK_HINT}\n  </style>"


def _fonts_block(template_id: str) -> str:
    return (
        "  <style data-superchat-fonts>\n"
        "/* Embedded fonts (Latin subset, SIL OFL 1.1): never read or edit this block. */\n"
        f"{_font_faces_css(template_id)}\n  </style>\n"
    )


def build_deck(template_id: str, title: str, slides_html: str | None = None) -> str:
    """The complete deck document for ``template_id`` with ``slides_html`` (or a sample cover)."""
    template = templates().get(template_id)
    if template is None:
        raise KitError(f"Unknown template '{template_id}'. Use one of: {', '.join(templates())}")
    slides = (slides_html or (KIT_DIR / "slides.html").read_text("utf-8")).strip("\n")
    skeleton = (KIT_DIR / "skeleton.html").read_text("utf-8")

    deck = skeleton.replace(
        "<title><!-- SLOT: deck title --></title>",
        f"<title>{html.escape(title.strip())}</title>",
        1,
    )
    root = _ROOT_RE.search(deck)
    if root is None:
        raise KitError("skeleton: theme token block not found")
    deck = deck[: root.start()] + _tokens_block(template) + deck[root.end() :]
    styles = _PLACEHOLDER_STYLE_RE.search(deck)
    if styles is None:
        raise KitError("skeleton: per-deck style block not found")
    deck = deck[: styles.start()] + _style_block(template) + deck[styles.end() :]
    region = re.search(
        r"      <!-- SLOT: slides.*?(?=    </div>\n  </div>\n\n  <!-- Framework chrome)",
        deck,
        re.S,
    )
    if region is None:
        raise KitError("skeleton: slide region not found")
    deck = (
        deck[: region.start()]
        + f"      {SLOTS_OPEN}\n{slides}\n      {SLOTS_CLOSE}\n\n"
        + deck[region.end() :]
    )
    deck = deck.replace("</head>", _chart_runtime() + "</head>", 1)
    return deck.replace("</body>", _fonts_block(template_id) + "</body>", 1)


def _head(document: str) -> str:
    """The document up to ``</head>``: where the kit's markers live (slides cannot fake them)."""
    end = document.find("</head>")
    return document if end < 0 else document[:end]


def deck_theme_id(document: str) -> str | None:
    """The id of the theme a built deck carries (from its marker), if it is one of ours."""
    head = _head(document)
    found = _THEME_ID_RE.search(head)
    if found is not None:
        return found.group(1) if found.group(1) in templates() else None
    legacy = _LEGACY_NAME_RE.search(head)
    if legacy is None:
        return None
    return next((t.id for t in templates().values() if t.name == legacy.group(1)), None)


_INEXACT = "this deck's theme can't be swapped exactly; ask Superchat to restyle it"
_EDITED = "this deck's theme block was edited between its markers; ask Superchat to restyle it"


def _only(
    pattern: re.Pattern[str], text: str, *, opens: str = "", closes: str = ""
) -> re.Match[str]:
    """The one match; zero or several matches, or stray or nested markers in the head, refuse."""
    scope = _head(text) if opens else text
    if (opens and scope.count(opens) != 1) or (closes and scope.count(closes) != 1):
        raise KitError(_INEXACT)
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise KitError(_INEXACT)
    return found[0]


@cache
def _legacy_kits() -> dict[str, list[dict[str, str]]]:
    """The block texts every released kit wrote before the markers existed, by theme id."""
    return json.loads((KIT_DIR / "legacy.json").read_text("utf-8"))


def _known_texts(kind: str) -> set[str]:
    """Every theme text the kit ever wrote (``kind`` is "tokens" or "css"): now and released."""
    now = _tokens_text if kind == "tokens" else _css_text
    old = [c[kind] for texts in _legacy_kits().values() for c in texts]
    return {now(t) for t in templates().values()} | set(old)


def _adopt_markers(document: str, template: Template) -> str:
    """A deck saved before the markers existed, with the kit's regions marked and the rest kept.

    The block is recognised against every released kit text for its theme (the kit has changed
    since); what the deck added around it stays in place. A block that is none of them was edited
    inside, and is refused rather than rewritten.
    """
    candidates = [
        *_legacy_kits().get(template.id, []),
        {"tokens": _tokens_text(template), "css": _css_text(template)},
    ]
    head = f"    /* {template.name} \u00b7 layout vocabulary and theme */\n"
    root = _only(_ROOT_RE, document)
    tokens = max(
        (c["tokens"] for c in candidates if c["tokens"] in root.group(0)), key=len, default=None
    )
    if tokens is None:
        raise KitError(_EDITED)
    document = (
        document[: root.start()]
        + root.group(0).replace(tokens, _tokens_region(template), 1)
        + document[root.end() :]
    )
    style = _only(_LEGACY_STYLE_RE, document)
    css = max(
        (c["css"] for c in candidates if head + c["css"] in style.group(0)), key=len, default=None
    )
    if css is None:
        raise KitError(_EDITED)
    block = style.group(0).replace(head + css, _css_region(template), 1)
    return document[: style.start()] + block + document[style.end() :]


def _refresh(
    document: str, pattern: re.Pattern[str], new_region: str, *, kind: str, opens: str, closes: str
) -> str:
    """Replace one kit region by the new theme's; a region edited between its markers refuses."""
    found = _only(pattern, document, opens=opens, closes=closes)
    sha, content = found.group(2), found.group(3)
    intact = sha == _digest(content) if sha else content in _known_texts(kind)
    if not intact:
        raise KitError(_EDITED)
    return document[: found.start()] + new_region + document[found.end() :]


def theme_source(document: str) -> str | None:
    """What ``artifact_outline(element="theme")`` reads: the deck's theme (id, name, light or
    dark), the kit's theme tokens, and the deck's own CSS after the theme (where an override
    goes); ``None`` for a document with no theme markers. Never the theme's long CSS."""
    tokens = _TOKENS_RE.search(document)
    css = _CSS_RE.search(document)
    marked = css if css is not None else tokens
    if marked is None:
        return None
    theme_id = marked.group(1)
    template = templates().get(theme_id)
    name = f"{template.name}, {template.mode} mode" if template is not None else "unknown"
    own = document[css.end() :].split("</style>", 1)[0].strip() if css is not None else ""
    return "\n".join(
        [
            f"Theme {theme_id} ({name}). Another look is deck_theme_set; a token override goes "
            "in the deck's own CSS, after the theme marker.",
            "Theme tokens (the kit's; never edit between the markers):",
            tokens.group(0) if tokens is not None else "(none)",
            "The deck's own CSS after the theme:",
            own or "(none)",
        ]
    )


def restyle_deck(document: str, template_id: str) -> str:
    """The deck with its theme swapped; the kit's regions and fonts change, all else stays.

    :raises KitError: for an unknown theme, a deck whose kit regions cannot be found exactly, or
        one edited between the markers; it is then restyled by asking the Muse.
    """
    template = templates().get(template_id)
    if template is None:
        raise KitError(f"Unknown theme '{template_id}'. Use one of: {', '.join(templates())}")
    if _THEME_ID_RE.search(_head(document)) is None:
        current = deck_theme_id(document)
        if current is None:
            raise KitError(_INEXACT)
        document = _adopt_markers(document, templates()[current])
    out = _refresh(
        document,
        _TOKENS_RE,
        _tokens_region(template),
        kind="tokens",
        opens="/* superchat:theme-tokens id=",
        closes="/* /superchat:theme-tokens */",
    )
    out = _refresh(
        out,
        _CSS_RE,
        _css_region(template),
        kind="css",
        opens="/* superchat:theme id=",
        closes="/* /superchat:theme */",
    )
    fonts = _only(_FONTS_RE, out)
    return out[: fonts.start()] + _fonts_block(template_id) + out[fonts.end() :]


def restyle_for_patch(source: str, theme: str) -> tuple[str, str]:
    """The ``set-theme`` hand edit: ``(new source, summary)``; installed into artifacts' patches.

    :raises ValueError: For an unknown theme.
    :raises AmbiguousEdit: When this deck can't be restyled exactly.
    """
    from omnigent.superchat.artifacts.patches import AmbiguousEdit

    template = templates().get(theme)
    if template is None:
        raise ValueError(f"Unknown theme '{theme}'")
    before = deck_theme_id(source)
    if before == theme:
        return source, ""
    try:
        new = restyle_deck(source, theme)
    except KitError as exc:
        raise AmbiguousEdit(
            f"Can't apply that edit exactly: {exc}; ask Superchat instead."
        ) from exc
    was = templates()[before].name if before else "a custom look"
    return new, f"switched the theme to {template.name} (was {was})"
