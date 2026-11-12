// Fixture slide in the pptx-generator skill's format: sync createSlide(pres, theme).
const slideConfig = { type: "cover", index: 1, title: "Quarterly review" };

function createSlide(pres, theme) {
  const slide = pres.addSlide();
  slide.background = { color: theme.bg };
  slide.addText(slideConfig.title, {
    x: 0.5,
    y: 2,
    w: 9,
    h: 1.2,
    fontSize: 48,
    fontFace: "Arial",
    color: theme.primary,
    bold: true,
    align: "center",
  });
  return slide;
}

module.exports = { createSlide, slideConfig };
