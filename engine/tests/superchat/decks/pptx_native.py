"""Native-object check for an exported .pptx: charts and tables must be PowerPoint objects.

Portions modified from nexu-io/open-design
plugins/community/humanize-ppt/scripts/pptx_qa.py@802708f, MIT (Copyright (c) 2026 LearnPrompt, see
that plugin's LICENSE). Changes: only the package walking (`_relationships`, `_resolve_target`,
`_ordered_slide_parts`) and the native-object count of ``inspect_pptx`` are kept (a ``graphicData``
whose uri ends ``/chart`` or ``/table``); the slide plan, placeholder, notes and transition checks
are not used here; ``native_slides`` also follows the chart relationship to the chart part.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"p": PML, "a": DRAWING, "c": CHART, "r": REL, "pr": PACKAGE_REL}

#: dom-to-pptx lays a 1920px slide on a 10in (9144000 EMU) page.
EMU_PER_PX = 9144000 / 1920


def read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _relationship_path(part_name: str) -> str:
    directory, filename = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def relationships(zf: zipfile.ZipFile, part_name: str) -> dict[str, dict[str, str]]:
    rels_name = _relationship_path(part_name)
    if rels_name not in zf.namelist():
        return {}
    root = read_xml(zf, rels_name)
    found: dict[str, dict[str, str]] = {}
    for rel in root.findall("pr:Relationship", NS):
        rel_id = rel.attrib.get("Id")
        if rel_id:
            found[rel_id] = dict(rel.attrib)
    return found


def resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def ordered_slide_parts(zf: zipfile.ZipFile) -> list[str]:
    presentation = "ppt/presentation.xml"
    root = read_xml(zf, presentation)
    rels = relationships(zf, presentation)
    parts: list[str] = []
    for slide_id in root.findall(".//p:sldIdLst/p:sldId", NS):
        rel_id = slide_id.attrib.get(f"{{{REL}}}id")
        target = rels.get(rel_id or "", {}).get("Target")
        if not target:
            raise ValueError(f"slide relationship is missing for {rel_id or 'unknown id'}")
        resolved = resolve_target(presentation, target)
        if resolved not in zf.namelist():
            raise ValueError(f"slide part is missing: {resolved}")
        parts.append(resolved)
    return parts


@dataclass
class NativeSlide:
    """What one slide holds as PowerPoint objects."""

    part: str
    charts: int = 0
    tables: int = 0
    pictures: int = 0
    chart_parts: list[str] = field(default_factory=list)

    @property
    def native_objects(self) -> int:
        return self.charts + self.tables


def native_slides(path: str) -> list[NativeSlide]:
    """Per slide: how many chart and table objects, pictures, and the chart parts it uses."""
    slides: list[NativeSlide] = []
    with zipfile.ZipFile(path) as zf:
        for part in ordered_slide_parts(zf):
            root = read_xml(zf, part)
            rels = relationships(zf, part)
            slide = NativeSlide(part=part, pictures=len(root.findall(".//p:pic", NS)))
            for node in root.findall(".//a:graphicData", NS):
                uri = node.attrib.get("uri", "")
                if uri.endswith("/table"):
                    slide.tables += 1
                if uri.endswith("/chart"):
                    slide.charts += 1
                    ref = node.find("c:chart", NS)
                    rel = rels.get(
                        ref.attrib.get(f"{{{REL}}}id", "") if ref is not None else "", {}
                    )
                    if rel.get("Target"):
                        slide.chart_parts.append(resolve_target(part, rel["Target"]))
            slides.append(slide)
    return slides


def pages_missing_native_objects(path: str, expected: list[int]) -> list[int]:
    """1-based slide numbers in ``expected`` that hold no chart or table object (humanize-ppt's
    ``pptx-native-object-missing`` finding)."""
    slides = native_slides(path)
    return [n for n in expected if n > len(slides) or slides[n - 1].native_objects == 0]
