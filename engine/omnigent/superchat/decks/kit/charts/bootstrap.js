// Superchat: Chart.js defaults for a deck. Runs right after the vendored Chart.js (before the slides' own
// DOMContentLoaded handlers, which create the charts).
// The canvas is painted at once: the PDF export prints it and the PowerPoint export reads the chart
// in its final state, so nothing may still be animating.
Chart.defaults.animation = false;
// A deck slide is 1920px wide; Chart.js' 12px text would be unreadable.
Chart.defaults.font.size = 24;
addEventListener("DOMContentLoaded", function () {
  // Charts use the slide's own typeface, which the PowerPoint export names on the native chart.
  var slide = document.querySelector(".slide");
  if (slide) Chart.defaults.font.family = getComputedStyle(slide).fontFamily;
});
