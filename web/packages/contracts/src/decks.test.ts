import { describe, expect, it } from "vitest";
import { deckSlideCount } from "./rpc/decks.js";

describe("deckSlideCount", () => {
  it("counts only real slide sections, not ones named in the framework's CSS or comments", () => {
    const html = `<html><head><style>
      /* edit slide content inside <section class="slide"> bodies */
    </style><script>const t = '<section class="slide">';</script></head><body>
      <!-- <section class="slide"> -->
      ${Array.from({ length: 5 }, (_, i) => `<section class="slide l-points" data-superchat-id="s${i}"></section>`).join("\n")}
    </body></html>`;
    expect(deckSlideCount(html)).toBe(5);
  });

  it("is 0 for a file without slides", () => {
    expect(deckSlideCount("<html><body><p>hi</p></body></html>")).toBe(0);
  });
});
