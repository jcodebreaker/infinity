const data = require("./data.json");
const slideConfig = { type: "content", index: 4, title: "Regions" };

function createSlide(pres, theme) {
  const slide = pres.addSlide();
  slide.background = { color: theme.bg };
  slide.addText(slideConfig.title, {
    x: 0.5,
    y: 0.3,
    w: 9,
    h: 0.7,
    fontSize: 32,
    fontFace: "Arial",
    color: theme.primary,
    bold: true,
  });
  const rows = data.table.map((row, r) =>
    row.map((text) => ({
      text,
      options:
        r === 0
          ? { bold: true, color: "FFFFFF", fill: { color: theme.primary } }
          : { color: theme.primary, fill: { color: "FFFFFF" } },
    })),
  );
  slide.addTable(rows, {
    x: 0.5,
    y: 1.4,
    w: 9,
    colW: [3, 3, 3],
    rowH: 0.45,
    fontFace: "Arial",
    fontSize: 16,
    border: { type: "solid", color: theme.light, pt: 1 },
  });
  slide.addText("4", {
    x: 9.3,
    y: 5.1,
    w: 0.4,
    h: 0.4,
    fontSize: 12,
    fontFace: "Arial",
    color: theme.primary,
    align: "center",
    valign: "middle",
  });
  return slide;
}

module.exports = { createSlide, slideConfig };
