import { describe, expect, it } from "vitest";
import { isEngineErrorText } from "./engine-error.js";

describe("isEngineErrorText", () => {
  it.each([
    "There's an issue with the selected model (x). It may not exist or you may not have access to it.",
    "Failed to call ${MODEL}",
    "Request failed: HTTP 529 overloaded",
    'API Error: 400 {"type":"invalid_request_error"}',
    "TypeError: x is not a function\n    at run (/app/a.js:10:5)",
  ])("flags %s", (text) => {
    expect(isEngineErrorText(text)).toBe(true);
  });

  it.each([
    "Your local time is 12:50 AM.",
    "A 404 page means the URL was not found, so I opened the home page instead.",
    "Use a code block:\n```js\nconst a = 1;\n```",
    `HTTP 500 means the server broke. ${"More detail. ".repeat(60)}`,
    "",
  ])("leaves %s alone", (text) => {
    expect(isEngineErrorText(text)).toBe(false);
  });
});

describe("model-layer failure copy", () => {
  it("maps a stored failure text back to its engine code, and only that text", async () => {
    const { MODEL_ERROR_COPY, modelErrorCode } = await import("./engine-error.js");
    for (const [code, copy] of Object.entries(MODEL_ERROR_COPY)) {
      expect(modelErrorCode(copy)).toBe(code);
      expect(modelErrorCode(`  ${copy}\n`)).toBe(code);
    }
    expect(modelErrorCode("Add your API key to continue, please")).toBeNull();
    expect(modelErrorCode("")).toBeNull();
  });
});
