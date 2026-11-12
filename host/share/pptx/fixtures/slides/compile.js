// The skill's compile.js (SKILL.md, Step 6) with the output path taken from SUPERCHAT_PPTX_OUT.
const pptxgen = require("pptxgenjs");
const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";

const theme = {
  primary: "22223b",
  secondary: "4a4e69",
  accent: "9a8c98",
  light: "c9ada7",
  bg: "f2e9e4",
};

for (let i = 1; i <= 4; i++) {
  const num = String(i).padStart(2, "0");
  require(`./slide-${num}.js`).createSlide(pres, theme);
}

pres.writeFile({ fileName: process.env.SUPERCHAT_PPTX_OUT || "./presentation.pptx" });
