// Quiet logs in tests.
process.env.LOG_LEVEL = "off";

// jsdom has no layout, so it has no hit testing: a component that asks which element is on top
// (a rAF callback that outlives its test) gets "nothing" instead of throwing.
if (typeof document !== "undefined" && typeof document.elementFromPoint !== "function") {
  document.elementFromPoint = () => null;
}
