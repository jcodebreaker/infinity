# ruff: noqa: E501  (the fixtures are HTML and Chart.js configs written on one line each)
"""Chart.js charts in a deck export as native PowerPoint charts (never a picture of the canvas).

Builds a deck with every chart type Chart.js and PptxGenJS share (bar clustered, stacked and
horizontal; line; area; pie; doughnut; radar; scatter; bubble; a bar with a line on a second axis),
a table, and text above and below each chart, runs the real ``superchat-deck-export`` helper in a
headless Chromium, and reads the .pptx back as a zip:

* each chart is a ``c:chart`` part with an embedded workbook, series values equal to the Chart.js
  data, and no ``p:pic`` anywhere;
* each chart's frame is within 1px of its canvas box, and z-order is kept (text under it stays
  under, text over it stays over);
* a table keeps its rendered row heights;
* ``deck_check`` (the helper's lint) names a chart that has no PowerPoint equivalent.

Skipped when no Chromium is installed.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from omnigent.superchat.decks import kit

from .pptx_native import (
    EMU_PER_PX,
    NS,
    native_slides,
    pages_missing_native_objects,
    read_xml,
    relationships,
    resolve_target,
)

HELPER = Path(__file__).resolve().parents[4] / "host/bin/superchat-deck-export"


def _chromium_available() -> bool:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.machinery as m, importlib.util as u;"
            f"l=m.SourceFileLoader('h','{HELPER}');s=u.spec_from_loader('h',l);x=u.module_from_spec(s);"
            "l.exec_module(x);x.find_chromium()",
        ],
        capture_output=True,
        cwd=str(HELPER.parent),
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not HELPER.is_file() or not _chromium_available(), reason="Chromium or the helper is missing"
)

BLUE, ORANGE, GREEN, PINK = "#1e2bfa", "#f26b3a", "#1b8a5a", "#c2185b"
BOX = {"x": 128, "y": 300, "w": 1200, "h": 480}
LABELS = ["Q1", "Q2", "Q3", "Q4"]
A = [10, 20, 0, 40]  # a zero must survive into the workbook and the chart
B = [15, 25, 35, 45]
OPTS = "responsive:true,maintainAspectRatio:false"


def _datasets(extra: str = "") -> str:
    return (
        f"datasets:[{{label:'Alpha',data:{A},backgroundColor:'{BLUE}',borderColor:'{BLUE}'{extra}}},"
        f"{{label:'Beta',data:{B},backgroundColor:'{ORANGE}',borderColor:'{ORANGE}'{extra}}}]"
    )


CASES: list[dict[str, Any]] = [
    {
        "id": "bar",
        "config": f"{{type:'bar',data:{{labels:{LABELS},{_datasets()}}},options:{{{OPTS},"
        "plugins:{legend:{position:'bottom'},title:{display:true,text:'Revenue'}},"
        "scales:{y:{min:0,max:60,ticks:{stepSize:20,format:{style:'percent'}}}}}}",
        "kind": "barChart",
        "series": [A, B],
        "names": ["Alpha", "Beta"],
        "contains": [
            '<c:legendPos val="b"/>',
            "Revenue",
            '<c:max val="60"/>',
            '<c:majorUnit val="20"/>',
            "0%",
        ],
        "colors": ["1E2BFA", "F26B3A"],
    },
    {
        "id": "stacked",
        "config": f"{{type:'bar',data:{{labels:{LABELS},{_datasets()}}},options:{{{OPTS},"
        "scales:{x:{stacked:true},y:{stacked:true}}}}",
        "kind": "barChart",
        "series": [A, B],
        "names": ["Alpha", "Beta"],
        "contains": ['<c:grouping val="stacked"/>', '<c:barDir val="col"/>'],
    },
    {
        "id": "horizontal",
        "config": f"{{type:'bar',data:{{labels:{LABELS},{_datasets()}}},options:{{{OPTS},indexAxis:'y'}}}}",
        "kind": "barChart",
        "series": [A, B],
        "names": ["Alpha", "Beta"],
        "contains": ['<c:barDir val="bar"/>'],
    },
    {
        "id": "line",
        "config": f"{{type:'line',data:{{labels:{LABELS},"
        f"{_datasets(',tension:.4,borderWidth:3,pointRadius:4')}}},options:{{{OPTS}}}}}",
        "kind": "lineChart",
        "series": [A, B],
        "names": ["Alpha", "Beta"],
        "contains": ['<c:smooth val="1"/>'],
    },
    {
        "id": "area",
        "config": f"{{type:'line',data:{{labels:{LABELS},datasets:[{{label:'Alpha',data:{A},"
        f"borderColor:'{BLUE}',backgroundColor:'{BLUE}33',fill:true}}]}},options:{{{OPTS}}}}}",
        "kind": "areaChart",
        "series": [A],
        "names": ["Alpha"],
        "contains": ['<a:alpha val="20000"/>'],
    },
    {
        "id": "pie",
        "config": f"{{type:'pie',data:{{labels:{LABELS},datasets:[{{label:'Share',data:{B},"
        f"backgroundColor:['{BLUE}','{ORANGE}','{GREEN}','{PINK}']}}]}},options:{{{OPTS}}}}}",
        "kind": "pieChart",
        "series": [B],
        "names": ["Share"],
        "colors": ["1E2BFA", "F26B3A", "1B8A5A", "C2185B"],
    },
    {
        "id": "doughnut",
        "config": f"{{type:'doughnut',data:{{labels:{LABELS},datasets:[{{label:'Share',data:{B},"
        f"backgroundColor:['{BLUE}','{ORANGE}','{GREEN}','{PINK}'],borderWidth:0}}]}},"
        f"options:{{{OPTS},cutout:'62%'}}}}",
        "kind": "doughnutChart",
        "series": [B],
        "names": ["Share"],
        "contains": ['<c:holeSize val="62"/>'],
    },
    {
        "id": "radar",
        "config": f"{{type:'radar',data:{{labels:{LABELS},{_datasets()}}},options:{{{OPTS},"
        "scales:{r:{suggestedMin:0,suggestedMax:50}}}}",
        "kind": "radarChart",
        "series": [A, B],
        "names": ["Alpha", "Beta"],
        "contains": ['<c:radarStyle val="filled"/>', '<c:max val="50"/>'],
    },
    {
        "id": "scatter",
        "config": "{type:'scatter',data:{datasets:["
        f"{{label:'One',data:[{{x:1,y:2}},{{x:2,y:4}},{{x:3,y:3}}],backgroundColor:'{BLUE}'}},"
        f"{{label:'Two',data:[{{x:1,y:1}},{{x:2,y:5}},{{x:3,y:2}}],backgroundColor:'{ORANGE}'}}]}},"
        f"options:{{{OPTS}}}}}",
        "kind": "scatterChart",
        "xs": [1, 2, 3],
        "ys": [[2, 4, 3], [1, 5, 2]],
        "names": ["One", "Two"],
    },
    {
        "id": "scatter-union",
        "config": "{type:'scatter',data:{datasets:["
        f"{{label:'One',data:[{{x:1,y:2}},{{x:2,y:4}}],backgroundColor:'{BLUE}'}},"
        f"{{label:'Two',data:[{{x:2.5,y:5}},{{x:3,y:2}}],backgroundColor:'{ORANGE}'}}]}},"
        f"options:{{{OPTS}}}}}",
        "kind": "scatterChart",
        "xs": [1, 2, 2.5, 3],
        "ys": [[2, 4, None, None], [None, None, 5, 2]],
        "names": ["One", "Two"],
    },
    {
        "id": "bubble",
        "config": "{type:'bubble',data:{datasets:["
        f"{{label:'One',data:[{{x:1,y:2,r:5}},{{x:2,y:3,r:10}},{{x:3,y:1,r:15}}],"
        f"backgroundColor:'{BLUE}99'}},"
        f"{{label:'Two',data:[{{x:1,y:3,r:8}},{{x:2,y:1,r:4}},{{x:3,y:4,r:12}}],"
        f"backgroundColor:'{ORANGE}99'}}]}},options:{{{OPTS}}}}}",
        "kind": "bubbleChart",
        "xs": [1, 2, 3],
        "ys": [[2, 3, 1], [3, 1, 4]],
        "sizes": [[5, 10, 15], [8, 4, 12]],
        "names": ["One", "Two"],
    },
    {
        "id": "combo",
        "config": f"{{type:'bar',data:{{labels:{LABELS},datasets:[{{type:'bar',label:'Orders',data:{B},"
        f"backgroundColor:'{BLUE}',yAxisID:'y'}},{{type:'line',label:'Rate',data:[2.1,2.2,2.2,2.4],"
        f"borderColor:'{ORANGE}',backgroundColor:'{ORANGE}',yAxisID:'y1'}}]}},"
        f"options:{{{OPTS},scales:{{y:{{position:'left'}},"
        "y1:{position:'right',grid:{drawOnChartArea:false}}}}}",
        "kind": ["barChart", "lineChart"],
        "series": [B, [2.1, 2.2, 2.2, 2.4]],
        "names": ["Orders", "Rate"],
        "axes": 4,
    },
]

UNDER = "Text under the chart"
OVER = "Text over the chart"


def _slide(index: int, case: dict[str, Any]) -> str:
    ident = case["id"]
    box = BOX
    active = " active" if index == 0 else ""
    return f"""<section class="slide{active} t-a" data-screen-label="{index + 1:02d} {ident}" data-superchat-id="{ident}">
  <h2 class="title" data-superchat-id="{ident}-title" style="position:absolute;left:128px;top:100px;z-index:1">{UNDER}</h2>
  <div class="chart" data-superchat-id="{ident}-chart" style="position:absolute;left:{box["x"]}px;top:{box["y"]}px;width:{box["w"]}px;height:{box["h"]}px;z-index:5"><canvas id="{ident}-canvas" data-superchat-id="{ident}-canvas"></canvas></div>
  <p class="lead" data-superchat-id="{ident}-over" style="position:absolute;left:200px;top:310px;z-index:9">{OVER}</p>
  <script data-superchat-chart>addEventListener('DOMContentLoaded',()=>{{new Chart(document.getElementById('{ident}-canvas'),{case["config"]});}});</script>
  <div class="foot" data-superchat-id="{ident}-foot"><span data-superchat-id="{ident}-foot-left">Deck</span><span data-superchat-id="{ident}-foot-page">{index + 1:02d}</span></div>
</section>"""


TABLE_ROWS_PX = [96, 64, 64]
TABLE_SLIDE = f"""<section class="slide t-a" data-screen-label="{len(CASES) + 1:02d} table" data-superchat-id="table">
  <table data-superchat-id="table-grid" style="position:absolute;left:128px;top:200px;width:1000px;border-collapse:collapse">
    <tr style="height:{TABLE_ROWS_PX[0]}px"><th data-superchat-id="t-h1" style="text-align:left">Region</th><th data-superchat-id="t-h2" style="text-align:left">Sales</th></tr>
    <tr style="height:{TABLE_ROWS_PX[1]}px"><td data-superchat-id="t-r1a">North</td><td data-superchat-id="t-r1b">120</td></tr>
    <tr style="height:{TABLE_ROWS_PX[2]}px"><td data-superchat-id="t-r2a">South</td><td data-superchat-id="t-r2b">95</td></tr>
  </table>
  <div class="foot" data-superchat-id="table-foot"><span data-superchat-id="table-foot-left">Deck</span><span data-superchat-id="table-foot-page">{len(CASES) + 1:02d}</span></div>
</section>"""


def _run(fmt: str, html: Path, out: Path | None = None) -> dict[str, Any]:
    args = [sys.executable, str(HELPER), "--format", fmt, "--html", str(html)]
    if out is not None:
        args += ["--out", str(out)]
    done = subprocess.run(args, capture_output=True, text=True, timeout=170, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("charts")
    slides = "\n".join([*(_slide(i, c) for i, c in enumerate(CASES)), TABLE_SLIDE])
    errors, _ = kit.check_slides(slides)
    assert errors == []
    deck = work / "charts.deck.html"
    deck.write_text(kit.build_deck("blue-professional", "Charts", slides), "utf-8")
    result = _run("pptx", deck, work / "charts.pptx")
    assert result["ok"] is True, result
    assert result["slides"] == len(CASES) + 1
    return work / "charts.pptx"


def _chart_xml(pptx: Path, slide_number: int) -> tuple[str, ET.Element, str]:
    """(chart part name, parsed chart, raw xml) of the only chart on a slide."""
    slide = native_slides(str(pptx))[slide_number - 1]
    assert slide.charts == 1, f"slide {slide_number} has {slide.charts} charts"
    with zipfile.ZipFile(pptx) as zf:
        raw = zf.read(slide.chart_parts[0]).decode("utf-8")
    return slide.chart_parts[0], ET.fromstring(raw), raw


def _numbers(parent: ET.Element | None) -> dict[int, float]:
    out: dict[int, float] = {}
    if parent is None:
        return out
    for pt in parent.findall(".//c:pt", NS):
        value = pt.find("c:v", NS)
        if value is not None and value.text not in (None, ""):
            out[int(pt.attrib["idx"])] = float(value.text or 0)
    return out


def _dense(points: dict[int, float], count: int) -> list[float | None]:
    return [points.get(i) for i in range(count)]


def _names(root: ET.Element) -> list[str]:
    names = []
    for ser in root.iter(f"{{{NS['c']}}}ser"):
        label = ser.find("c:tx//c:v", NS)
        names.append((label.text or "") if label is not None else "")
    return names


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_chart_is_a_native_chart_with_the_chartjs_data(
    exported: Path, case: dict[str, Any]
) -> None:
    number = CASES.index(case) + 1
    part, root, raw = _chart_xml(exported, number)
    kinds = case["kind"] if isinstance(case["kind"], list) else [case["kind"]]
    for kind in kinds:
        assert f"<c:{kind}>" in raw, f"{case['id']}: no {kind} in {part}"
    assert _names(root) == case["names"]
    sers = list(root.iter(f"{{{NS['c']}}}ser"))
    if "series" in case:
        got = [
            _dense(_numbers(s.find("c:val", NS)), len(case["series"][i]))
            for i, s in enumerate(sers)
        ]
        assert got == case["series"]
    else:
        xs = case["xs"]
        assert _dense(_numbers(sers[0].find("c:xVal", NS)), len(xs)) == xs
        assert [_dense(_numbers(s.find("c:yVal", NS)), len(xs)) for s in sers] == case["ys"]
        if "sizes" in case:
            sizes = [_dense(_numbers(s.find("c:bubbleSize", NS)), len(xs)) for s in sers]
            assert sizes == case["sizes"]
    for needle in case.get("contains", []):
        assert needle in raw, f"{case['id']}: {needle!r} missing from {part}"
    for color in case.get("colors", []):
        assert f'<a:srgbClr val="{color}"' in raw
    if "axes" in case:
        assert raw.count("<c:valAx>") + raw.count("<c:catAx>") == case["axes"]


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_chart_has_an_embedded_workbook_with_the_data(
    exported: Path, case: dict[str, Any]
) -> None:
    number = CASES.index(case) + 1
    part, _root, _raw = _chart_xml(exported, number)
    with zipfile.ZipFile(exported) as zf:
        rels = relationships(zf, part)
        targets = [r["Target"] for r in rels.values() if r["Target"].endswith(".xlsx")]
        assert len(targets) == 1, f"{part} has no embedded workbook"
        book = resolve_target(part, targets[0])
        workbook = zipfile.ZipFile(io.BytesIO(zf.read(book)))
        sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    values = [float(v) for v in re.findall(r"<v>([^<]*)</v>", sheet) if _is_number(v)]
    expected = case.get("series") or [case["xs"], *case["ys"]]
    for series in expected:
        for value in series:
            if value is not None:
                assert float(value) in values, f"{case['id']}: {value} missing from the workbook"
    # The zero is a real 0 in Edit Data, not an empty cell.
    if case["id"] in ("bar", "line"):
        assert "<v></v>" not in sheet


def test_no_slide_holds_a_picture_and_every_chart_slide_a_chart(exported: Path) -> None:
    slides = native_slides(str(exported))
    assert [s.pictures for s in slides] == [0] * len(slides)
    assert pages_missing_native_objects(str(exported), list(range(1, len(slides) + 1))) == []
    assert [s.charts for s in slides[: len(CASES)]] == [1] * len(CASES)
    assert slides[-1].tables == 1


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_chart_frame_is_the_canvas_box_and_z_order_is_kept(
    exported: Path, case: dict[str, Any]
) -> None:
    number = CASES.index(case) + 1
    with zipfile.ZipFile(exported) as zf:
        root = read_xml(zf, native_slides(str(exported))[number - 1].part)
    tree = root.find(".//p:spTree", NS)
    assert tree is not None
    order = []
    frame = None
    for child in tree:
        text = "".join(t.text or "" for t in child.iter(f"{{{NS['a']}}}t"))
        if UNDER in text:
            order.append("under")
        elif OVER in text:
            order.append("over")
        elif child.tag.endswith("graphicFrame"):
            order.append("chart")
            frame = child
    assert order == ["under", "chart", "over"]
    assert frame is not None
    off, ext = frame.find(".//p:xfrm/a:off", NS), frame.find(".//p:xfrm/a:ext", NS)
    assert off is not None and ext is not None
    got = (
        int(off.attrib["x"]),
        int(off.attrib["y"]),
        int(ext.attrib["cx"]),
        int(ext.attrib["cy"]),
    )
    for have, want in zip(got, (BOX["x"], BOX["y"], BOX["w"], BOX["h"]), strict=True):
        assert abs(have - want * EMU_PER_PX) <= EMU_PER_PX, f"{case['id']}: {have} vs {want}px"
    name = frame.find(".//p:cNvPr", NS)
    assert name is not None and name.attrib["name"].startswith("Chart")


def test_table_keeps_its_rendered_row_heights(exported: Path) -> None:
    slide = native_slides(str(exported))[-1]
    with zipfile.ZipFile(exported) as zf:
        root = read_xml(zf, slide.part)
    heights = [int(tr.attrib["h"]) for tr in root.iter(f"{{{NS['a']}}}tr")]
    assert len(heights) == len(TABLE_ROWS_PX)
    for got, want in zip(heights, TABLE_ROWS_PX, strict=True):
        assert abs(got - want * EMU_PER_PX) <= EMU_PER_PX, f"{got} vs {want}px"


# ----------------------------------------------------------------------------- deck_check


def _one(ident: str, config: str) -> str:
    return f"""<section class="slide active t-a" data-screen-label="01 {ident}" data-superchat-id="{ident}">
  <div class="chart" data-superchat-id="{ident}-chart" style="position:absolute;left:128px;top:300px;width:900px;height:400px"><canvas id="{ident}-canvas" data-superchat-id="{ident}-canvas"></canvas></div>
  <script data-superchat-chart>addEventListener('DOMContentLoaded',()=>{{new Chart(document.getElementById('{ident}-canvas'),{config});}});</script>
  <div class="foot" data-superchat-id="{ident}-foot"><span data-superchat-id="{ident}-foot-left">Deck</span><span data-superchat-id="{ident}-foot-page">01</span></div>
</section>"""


def _lint(slides: str, tmp_path: Path) -> dict[str, Any]:
    deck = tmp_path / "check.deck.html"
    deck.write_text(kit.build_deck("blue-professional", "Check", slides), "utf-8")
    return _run("lint", deck)


def test_check_rejects_a_chart_without_a_powerpoint_equivalent(tmp_path: Path) -> None:
    config = f"{{type:'polarArea',data:{{labels:{LABELS},datasets:[{{data:{B}}}]}}}}"
    report = _lint(_one("polar", config), tmp_path)
    errors = [i for i in report["issues"] if i["severity"] == "error"]
    assert len(errors) == 1 and errors[0]["code"] == "chart-not-native"
    assert "polarArea" in errors[0]["message"] and "table" in errors[0]["message"]
    assert errors[0]["id"] == "polar-canvas"


def test_check_rejects_a_mix_that_cannot_share_one_powerpoint_chart(tmp_path: Path) -> None:
    config = (
        f"{{type:'bar',data:{{labels:{LABELS},"
        f"datasets:[{{type:'bar',data:{A}}},{{type:'radar',data:{B}}}]}}}}"
    )
    report = _lint(_one("mix", config), tmp_path)
    messages = [i["message"] for i in report["issues"] if i["code"] == "chart-not-native"]
    assert messages and "cannot share a chart" in messages[0]


def test_check_accepts_a_native_chart_and_the_export_refuses_the_rest(tmp_path: Path) -> None:
    fine = _one("fine", f"{{type:'bar',data:{{labels:{LABELS},datasets:[{{data:{A}}}]}}}}")
    ok = _lint(fine, tmp_path)
    assert [i for i in ok["issues"] if i["severity"] == "error"] == []
    bad = tmp_path / "bad.deck.html"
    polar = _one("polar", f"{{type:'polarArea',data:{{labels:{LABELS},datasets:[{{data:{B}}}]}}}}")
    bad.write_text(kit.build_deck("blue-professional", "Bad", polar), "utf-8")
    refused = _run("pptx", bad, tmp_path / "bad.pptx")
    assert refused["ok"] is False and "polarArea" in str(refused["error"])
    assert not (tmp_path / "bad.pptx").exists()


def test_slides_may_carry_only_chart_scripts() -> None:
    plain = _one("x", "{type:'bar',data:{labels:['a'],datasets:[{data:[1]}]}}")
    assert kit.check_slides(plain)[0] == []
    other = plain.replace("<script data-superchat-chart>", "<script>")
    assert any("<script>" in e for e in kit.check_slides(other)[0])
    fetching = _one("x", "{type:'bar',data:{labels:['a'],datasets:[{data:fetch('/x')}]}}")
    assert any("nothing is fetched" in e for e in kit.check_slides(fetching)[0])
    empty = plain.replace("new Chart(", "console.log(")
    assert any("must create a Chart.js chart" in e for e in kit.check_slides(empty)[0])
