"""A saved page gets ids on what a person may edit by hand, so the editor can type into it."""

import re

from omnigent.superchat.artifacts.patches import SetText, apply_patches, name_elements

PAGE = (
    b'<html><head><style>h3{color:red}</style></head><body><section id="about">'
    b'<h2>About Morocco</h2><div class="grid">'
    b'<div class="card" data-superchat-id="tea"><svg><path d="M0"/></svg><h3>Zellige</h3></div>'
    b'<div class="card"><div class="glyph"></div><h3>Arches</h3>'
    b"<p>Horseshoe &amp; lobed.</p></div>"
    b'<div class="card"><h3>Arches</h3></div><span>plain</span></div></section></body></html>'
)


def test_names_text_and_boxes_from_their_words_and_classes():
    out = name_elements(PAGE).decode()
    for named in (
        '<section id="about" data-superchat-id="section-about-morocco">',
        '<h2 data-superchat-id="h2-about-morocco">',
        '<div class="card" data-superchat-id="card-arches-horseshoe">',
        '<h3 data-superchat-id="h3-arches">',
        '<p data-superchat-id="p-horseshoe-lobed">',
        '<h3 data-superchat-id="h3-arches-2">',
    ):
        assert named in out


def test_keeps_existing_ids_and_skips_svg_insides_style_and_spans():
    out = name_elements(PAGE).decode()
    assert '<div class="card" data-superchat-id="tea">' in out
    assert '<path d="M0"/>' in out
    assert "<style>h3{color:red}</style>" in out
    assert "<span>plain</span>" in out


def test_only_inserts_attributes_and_is_stable():
    out = name_elements(PAGE)
    assert re.sub(rb' data-superchat-id="[^"]*"', b"", out).replace(
        b' data-superchat-id="tea"', b""
    ) == PAGE.replace(b' data-superchat-id="tea"', b"")
    assert name_elements(out) == out


def test_named_text_can_then_be_edited_by_hand():
    out, _ = apply_patches(
        name_elements(PAGE), [SetText(kind="set-text", id="h3-arches", text="Gates")]
    )
    assert b">Gates</h3>" in out


def test_leaves_non_utf8_alone():
    assert name_elements(b"\xff<p>x</p>") == b"\xff<p>x</p>"
