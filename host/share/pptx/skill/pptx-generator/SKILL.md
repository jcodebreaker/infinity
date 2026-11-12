---
name: pptx-generator
description: "Generate, edit, and read PowerPoint presentations. Create from scratch with PptxGenJS (cover, TOC, content, section divider, summary slides), edit existing PPTX via XML workflows, or extract text with markitdown. Triggers: PPT, PPTX, PowerPoint, presentation, slide, deck, slides."
license: MIT
metadata:
  version: "1.0"
  category: productivity
  sources:
    - https://gitbrent.github.io/PptxGenJS/
    - https://github.com/microsoft/markitdown
---

<!-- Portions from MiniMax-AI/skills skills/pptx-generator/SKILL.md@60aaae5, MIT; changes: Superchat host paths and runtime (preinstalled libraries, NODE_PATH), artifact_save delivery, chart and table data rules, QA commands; the pipeline, rules and page-type guidance are unchanged. See NOTICE.md and LICENSE next to this file. -->

# PPTX Generator & Editor

## Overview

This skill handles all PowerPoint tasks: reading/analyzing existing presentations, editing template-based decks via XML manipulation, and creating presentations from scratch using PptxGenJS. It includes a complete design system (color palettes, fonts, style recipes) and detailed guidance for every slide type.

## In Superchat's host (read this first)

Everything below is MiniMax's pipeline. These rules adapt it to the host and override the
upstream text where they differ.

- **Preinstalled, nothing to install.** `require('pptxgenjs')` (4.0.1, patched for the chart
  workbook table ref and the dangling chart axis id), `react`, `react-dom`, `react-icons` and
  `sharp` resolve from any script because `NODE_PATH` is set. `python3 -m markitdown` and
  `python-pptx` are installed too. The host cannot install packages: never run `npm install`
  or `pip install`, and use only these libraries.
- **Work area.** Keep the slide modules, `compile.js` and `imgs/` in a scratch folder
  (`/tmp/deck-<slug>/slides/`). Write the final `.pptx` straight into its deliverable folder
  (see the workspace rules: `~/workspace/your_files/<name>.pptx`, or the Goal's `files/`) by
  setting `fileName` in `compile.js`; never copy it there afterwards.
- **Deliver with `artifact_save`** on that path once QA is clean. That puts the `.pptx` in the
  person's Library with a file card that opens a preview in Superchat. Reply in one line. Saving the
  same name again adds a version, so revise in place.
- **Numbers come from the person's data.** Never invent a figure for a chart, table or stat
  callout. Read the person's files, compute every value with pandas or duckdb in the host
  (say briefly what you computed), and pass the computed arrays to `addChart` / `addTable`. If
  the data does not exist, leave the chart out or ask for it; a slide without a chart beats a
  made-up one.
- **Charts and tables are native.** Use `slide.addChart(pres.charts.BAR, ...)` (or LINE, PIE, ...)
  and `slide.addTable(...)` so the person can open Edit Data in PowerPoint. Never draw a chart as
  an image, an SVG or shapes, and never paste a chart screenshot. Give every chart a title or a
  caption that names what it shows and its unit. Where the page-type guides below mention an SVG
  chart, use a native chart instead.
- **Tables: always pass `rowH`** (one number, in inches, big enough for the text) next to `colW`,
  so every row has an explicit height; Superchat's preview clips a table to its frame otherwise.
- **Text extraction.** Where this skill says `python -m markitdown`, run `python3 -m markitdown`.

## Quick Reference

| Task | Approach |
|------|----------|
| Read/analyze content | `python -m markitdown presentation.pptx` |
| Edit or create from template | See [Editing Presentations](references/editing.md) |
| Create from scratch | See [Creating from Scratch](#creating-from-scratch-workflow) below |

| Item | Value |
|------|-------|
| **Dimensions** | 10" x 5.625" (LAYOUT_16x9) |
| **Colors** | 6-char hex without # (e.g., `"FF0000"`) |
| **English font** | Arial (default), or approved alternatives |
| **Chinese font** | Microsoft YaHei |
| **Page badge position** | x: 9.3", y: 5.1" |
| **Theme keys** | `primary`, `secondary`, `accent`, `light`, `bg` |
| **Shapes** | RECTANGLE, OVAL, LINE, ROUNDED_RECTANGLE |
| **Charts** | BAR, LINE, PIE, DOUGHNUT, SCATTER, BUBBLE, RADAR |

## Reference Files

| File | Contents |
|------|----------|
| [slide-types.md](references/slide-types.md) | 5 slide page types (Cover, TOC, Section Divider, Content, Summary) + additional layout patterns |
| [design-system.md](references/design-system.md) | Color palettes, font reference, style recipes (Sharp/Soft/Rounded/Pill), typography & spacing |
| [editing.md](references/editing.md) | Template-based editing workflow, XML manipulation, formatting rules, common pitfalls |
| [pitfalls.md](references/pitfalls.md) | QA process, common mistakes, critical PptxGenJS pitfalls |
| [pptxgenjs.md](references/pptxgenjs.md) | Complete PptxGenJS API reference |

---

## Reading Content

```bash
# Text extraction
python -m markitdown presentation.pptx
```

---

## Creating from Scratch — Workflow

**Use when no template or reference presentation is available.**

### Step 1: Research & Requirements

Search to understand user requirements — topic, audience, purpose, tone, content depth.

### Step 2: Select Color Palette & Fonts

Use the [Color Palette Reference](references/design-system.md#color-palette-reference) to select a palette matching the topic and audience. Use the [Font Reference](references/design-system.md#font-reference) to choose a font pairing.

### Step 3: Select Design Style

Use the [Style Recipes](references/design-system.md#style-recipes) to choose a visual style (Sharp, Soft, Rounded, or Pill) matching the presentation tone.

### Step 4: Plan Slide Outline

Classify **every slide** as exactly one of the [5 page types](references/slide-types.md). Plan the content and layout for each slide. Ensure visual variety — do NOT repeat the same layout across slides.

### Step 5: Generate Slide JS Files

Create one JS file per slide in `slides/` directory. Each file must export a synchronous `createSlide(pres, theme)` function. Follow the [Slide Output Format](#slide-output-format) and the type-specific guidance in [slide-types.md](references/slide-types.md). Generate up to 5 slides concurrently using subagents if available.

**Tell each subagent:**
1. File naming: `slides/slide-01.js`, `slides/slide-02.js`, etc.
2. Images go in: `slides/imgs/`
3. The final PPTX is written only by `compile.js`, into its deliverable folder
4. Dimensions: 10" x 5.625" (LAYOUT_16x9)
5. Fonts: Chinese = Microsoft YaHei, English = Arial (or approved alternative)
6. Colors: 6-char hex without # (e.g. `"FF0000"`)
7. Must use the theme object contract (see [Theme Object Contract](#theme-object-contract))
8. Must follow the [PptxGenJS API reference](references/pptxgenjs.md)

### Step 6: Compile into Final PPTX

Create `slides/compile.js` to combine all slide modules (it writes the final file straight into its deliverable folder):

```javascript
// slides/compile.js
const pptxgen = require('pptxgenjs');
const pres = new pptxgen();
pres.layout = 'LAYOUT_16x9';

const theme = {
  primary: "22223b",    // dark color for backgrounds/text
  secondary: "4a4e69",  // secondary accent
  accent: "9a8c98",     // highlight color
  light: "c9ada7",      // light accent
  bg: "f2e9e4"          // background color
};

for (let i = 1; i <= 12; i++) {  // adjust count as needed
  const num = String(i).padStart(2, '0');
  const slideModule = require(`./slide-${num}.js`);
  slideModule.createSlide(pres, theme);
}

// the final file goes straight into its folder; name it for the task
pres.writeFile({ fileName: `${process.env.HOME}/workspace/your_files/presentation.pptx` });
```

Run with: `cd /tmp/deck-<slug>/slides && node compile.js`

### Step 7: QA (Required)

See [QA Process](references/pitfalls.md#qa-process).

### Output Structure

```
/tmp/deck-<slug>/slides/          # scratch, thrown away
├── slide-01.js                   # Slide modules
├── slide-02.js
├── ...
├── imgs/                         # Images used in slides
└── compile.js                    # writes the .pptx into ~/workspace/your_files/
```

---

## Slide Output Format

Each slide is a **complete, runnable JS file**:

```javascript
// slide-01.js
const pptxgen = require("pptxgenjs");

const slideConfig = {
  type: 'cover',
  index: 1,
  title: 'Presentation Title'
};

// MUST be synchronous (not async)
function createSlide(pres, theme) {
  const slide = pres.addSlide();
  slide.background = { color: theme.bg };

  slide.addText(slideConfig.title, {
    x: 0.5, y: 2, w: 9, h: 1.2,
    fontSize: 48, fontFace: "Arial",
    color: theme.primary, bold: true, align: "center"
  });

  return slide;
}

// Standalone preview - use slide-specific filename
if (require.main === module) {
  const pres = new pptxgen();
  pres.layout = 'LAYOUT_16x9';
  const theme = {
    primary: "22223b",
    secondary: "4a4e69",
    accent: "9a8c98",
    light: "c9ada7",
    bg: "f2e9e4"
  };
  createSlide(pres, theme);
  pres.writeFile({ fileName: "slide-01-preview.pptx" });
}

module.exports = { createSlide, slideConfig };
```

---

## Theme Object Contract (MANDATORY)

The compile script passes a theme object with these **exact keys**:

| Key | Purpose | Example |
|-----|---------|---------|
| `theme.primary` | Darkest color, titles | `"22223b"` |
| `theme.secondary` | Dark accent, body text | `"4a4e69"` |
| `theme.accent` | Mid-tone accent | `"9a8c98"` |
| `theme.light` | Light accent | `"c9ada7"` |
| `theme.bg` | Background color | `"f2e9e4"` |

**NEVER use other key names** like `background`, `text`, `muted`, `darkest`, `lightest`.

---

## Page Number Badge (REQUIRED)

All slides **except Cover Page** MUST include a page number badge in the bottom-right corner.

- **Position**: x: 9.3", y: 5.1"
- Show current number only (e.g. `3` or `03`), NOT "3/12"
- Use palette colors, keep subtle

### Circle Badge (Default)

```javascript
slide.addShape(pres.shapes.OVAL, {
  x: 9.3, y: 5.1, w: 0.4, h: 0.4,
  fill: { color: theme.accent }
});
slide.addText("3", {
  x: 9.3, y: 5.1, w: 0.4, h: 0.4,
  fontSize: 12, fontFace: "Arial",
  color: "FFFFFF", bold: true,
  align: "center", valign: "middle"
});
```

### Pill Badge

```javascript
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 9.1, y: 5.15, w: 0.6, h: 0.35,
  fill: { color: theme.accent },
  rectRadius: 0.15
});
slide.addText("03", {
  x: 9.1, y: 5.15, w: 0.6, h: 0.35,
  fontSize: 11, fontFace: "Arial",
  color: "FFFFFF", bold: true,
  align: "center", valign: "middle"
});
```

---

## Dependencies

Preinstalled in the host image (see the first section): `pptxgenjs`, `react`, `react-dom`,
`react-icons`, `sharp`, `markitdown[pptx]`. Install nothing.
