const data = require("./data.json");
const slideConfig = { type: "content", index: 2, title: "Revenue by quarter" };

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
  slide.addChart(pres.charts.BAR, [data.revenue], {
    x: 0.5,
    y: 1.2,
    w: 9,
    h: 3.8,
    barDir: "col",
    chartColors: [theme.secondary],
    showValue: true,
    showLegend: false,
    showTitle: true,
    title: "Revenue (k) per quarter",
  });
  slide.addText("2", {
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
