const data = require("./data.json");
const slideConfig = { type: "content", index: 3, title: "Active users" };

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
  slide.addChart(pres.charts.LINE, [data.trend], {
    x: 0.5,
    y: 1.2,
    w: 5.6,
    h: 3.8,
    lineSize: 3,
    lineSmooth: true,
    chartColors: [theme.accent],
    showLegend: false,
    showTitle: true,
    title: "Active users per month",
  });
  slide.addChart(pres.charts.PIE, [data.share], {
    x: 6.2,
    y: 1.2,
    w: 3.4,
    h: 3.8,
    showPercent: true,
    chartColors: [theme.primary, theme.secondary, theme.light],
    showLegend: true,
    legendPos: "b",
    showTitle: true,
    title: "Share of revenue",
  });
  slide.addText("3", {
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
