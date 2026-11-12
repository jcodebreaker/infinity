// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import { applyMuseProductMode } from "./product-mode";

afterEach(() => {
  delete document.documentElement.dataset.product;
});

it("marks the page for the Muse palette", () => {
  applyMuseProductMode();
  expect(document.documentElement.dataset.product).toBe("muse");
});
