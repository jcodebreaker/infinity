// Regenerates legend-colors.pptx: a pie, a doughnut and a varyColors bar chart written by the same
// patched PptxGenJS 4.0.1 the host image carries (host/share/pptx/runtime).
//   node make-legend-fixture.cjs
const path = require("node:path");
const PptxGenJS = require(
  path.resolve(__dirname, "../../../../../../host/share/pptx/runtime/node_modules/pptxgenjs"),
);

const labels = ["North", "South", "East", "West"];
const slice = [{ name: "Revenue", labels, values: [40, 30, 20, 10] }];
// Deliberately far from any theme accent colour.
const colors = ["C0392B", "27AE60", "8E44AD", "F1C40F"];

const pres = new PptxGenJS();
pres.layout = "LAYOUT_16x9";
const options = (extra) => ({
  x: 0.5,
  y: 0.5,
  w: 8.5,
  h: 4.5,
  showLegend: true,
  legendPos: "r",
  chartColors: colors,
  ...extra,
});
pres.addSlide().addChart(pres.charts.PIE, slice, options({}));
pres.addSlide().addChart(pres.charts.DOUGHNUT, slice, options({}));
pres.addSlide().addChart(pres.charts.BAR, slice, options({ barDir: "col" }));
pres
  .writeFile({ fileName: path.join(__dirname, "legend-colors.pptx") })
  .then(() => console.log("ok"));
