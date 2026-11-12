// @vitest-environment node
// The edit bridge in a real headless Chromium: it runs inside a built deck (the kit's skeleton, so
// the deck's own click and key handlers are live) and talks to `window` itself, since a top-level
// page is its own parent. Skipped when no Chromium can be launched.
import { readFileSync } from "node:fs";
import { type Browser, chromium, type Page } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { SandboxedHtmlViewer } from "../../../../components/SandboxedHtmlViewer";
import { injectEditBridge } from "./bridge-script";

const NONCE = "0123456789abcdef0123456789abcdef";
const DECK = readFileSync(new URL("./sample-deck.txt", import.meta.url), "utf8");

let browser: Browser | undefined;
beforeAll(async () => {
  try {
    browser = await chromium.launch();
  } catch {
    browser = undefined;
  }
});
afterAll(async () => {
  await browser?.close();
});

type Msg = Record<string, unknown> & { type: string };

async function open(): Promise<Page> {
  const page = await browser!.newPage({ viewport: { width: 1280, height: 720 } });
  await page.addInitScript(() => {
    const w = window as unknown as { __msgs: unknown[] };
    w.__msgs = [];
    window.addEventListener("message", (e) => w.__msgs.push(e.data));
  });
  await page.route("http://deck.test/", (route) =>
    route.fulfill({ contentType: "text/html", body: injectEditBridge(DECK, NONCE) }),
  );
  await page.goto("http://deck.test/");
  await page.waitForFunction(() =>
    (window as unknown as { __msgs: Msg[] }).__msgs.some((m) => m.type === "superchat:edit-ready"),
  );
  return page;
}
// postMessage is asynchronous: let the queue drain before reading what arrived.
const msgs = async (page: Page, type: string) => {
  await page.waitForTimeout(60);
  return page.evaluate(
    (t) => (window as unknown as { __msgs: Msg[] }).__msgs.filter((m) => m.type === t),
    type,
  );
};
const last = async (page: Page, type: string) => (await msgs(page, type)).at(-1);
const host = (page: Page, msg: Record<string, unknown>) =>
  page.evaluate((m) => window.postMessage(m, "*"), { v: 1, nonce: NONCE, ...msg });
const activeIndex = (page: Page) =>
  page.evaluate(() =>
    [...document.querySelectorAll(".slide")].findIndex((s) => s.classList.contains("active")),
  );
const targets = async (page: Page) =>
  ((await last(page, "superchat:edit-selection"))?.targets ?? []) as Array<Record<string, any>>;

describe("edit bridge (Chromium)", () => {
  it("announces the theme: colour tokens and font tokens", async () => {
    if (!browser) return;
    const page = await open();
    const ready = (await last(page, "superchat:edit-ready")) as Msg & { theme: any };
    expect(ready.theme.fonts.map((f: any) => f.label)).toContain("Space Grotesk");
    expect(ready.theme.colors.map((c: any) => c.name)).toContain("--accent");
    expect(ready.theme.slideCount).toBeGreaterThan(2);
    await page.close();
  });

  it("selects on click without navigating, multi-selects with shift, Esc deselects", async () => {
    if (!browser) return;
    const page = await open();
    await page.click('[data-superchat-id="cover-title"]');
    expect((await targets(page)).map((t) => t.id)).toEqual(["cover-title"]);
    expect((await targets(page))[0]).toMatchObject({ kind: "text", editable: true, slide: 1 });
    expect(await activeIndex(page)).toBe(0); // the right half would have gone to slide 2
    await page.click('[data-superchat-id="cover-lead"]', { modifiers: ["Shift"] });
    expect((await targets(page)).map((t) => t.id)).toEqual(["cover-title", "cover-lead"]);
    await page.keyboard.press("Escape");
    expect(await targets(page)).toEqual([]);
    await page.keyboard.press("ArrowRight"); // no selection: the deck navigates again
    expect(await activeIndex(page)).toBe(1);
    await page.close();
  });

  it("edits text inline: Enter commits, Esc cancels, runs and styling stay", async () => {
    if (!browser) return;
    const page = await open();
    const sel = '[data-superchat-id="cover-title"]';
    const classBefore = await page.getAttribute(sel, "class");
    await page.dblclick(sel);
    expect(await page.getAttribute(sel, "contenteditable")).toBe("plaintext-only");
    await page.keyboard.press("Meta+A");
    await page.keyboard.type("Shipping is fast");
    await page.keyboard.press("Enter");
    expect(await page.getAttribute(sel, "contenteditable")).toBeNull();
    const commit = (await last(page, "superchat:edit-text-commit")) as Msg;
    expect(commit).toMatchObject({ id: "cover-title", text: "Shipping is fast" });
    expect(await page.getAttribute(sel, "class")).toBe(classBefore);

    await page.keyboard.press("Enter"); // selected element: Enter starts editing
    expect(await page.getAttribute(sel, "contenteditable")).toBe("plaintext-only");
    await page.keyboard.type("zzz");
    await page.keyboard.press("Escape");
    expect(await page.textContent(sel)).toBe("Shipping is fast");
    expect(await msgs(page, "superchat:edit-text-commit")).toHaveLength(1);
    await page.close();
  });

  it("nudges with arrows instead of navigating, and forwards undo/delete keys", async () => {
    if (!browser) return;
    const page = await open();
    await page.click('[data-superchat-id="cover-meta"]');
    await page.keyboard.press("Shift+ArrowRight");
    expect(await last(page, "superchat:edit-key")).toMatchObject({
      action: "nudge",
      dx: 10,
      dy: 0,
    });
    expect(await activeIndex(page)).toBe(0);
    await page.keyboard.press("Delete");
    expect(await last(page, "superchat:edit-key")).toMatchObject({ action: "delete" });
    await page.keyboard.press("Meta+z");
    expect(await last(page, "superchat:edit-key")).toMatchObject({ action: "undo" });
    await page.close();
  });

  it("previews styles from the host and reports the new geometry", async () => {
    if (!browser) return;
    const page = await open();
    await page.click('[data-superchat-id="cover-lead"]');
    const before = (await targets(page))[0]!;
    await host(page, {
      type: "superchat:edit-preview",
      id: "cover-lead",
      style: { "font-size": "70px", "letter-spacing": "2px" },
    });
    await page.waitForFunction(() =>
      document
        .querySelector('[data-superchat-id="cover-lead"]')
        ?.getAttribute("style")
        ?.includes("70px"),
    );
    const after = (await targets(page))[0]!;
    expect(after.computed["font-size"]).toBe("70px");
    expect(after.inline["font-size"]).toBe("70px");
    expect(after.rect.h).toBeGreaterThanOrEqual(before.rect.h);
    await page.close();
  });

  it("ignores messages with a wrong nonce or version", async () => {
    if (!browser) return;
    const page = await open();
    await page.evaluate(() =>
      window.postMessage(
        { type: "superchat:edit-select", v: 1, nonce: "nope", ids: ["cover-title"] },
        "*",
      ),
    );
    await page.evaluate(() =>
      window.postMessage(
        { type: "superchat:edit-select", v: 2, nonce: "x", ids: ["cover-title"] },
        "*",
      ),
    );
    await page.waitForTimeout(100);
    expect(await msgs(page, "superchat:edit-selection")).toHaveLength(0);
    await host(page, { type: "superchat:edit-select", ids: ["cover-title"] });
    await page.waitForFunction(() =>
      (window as any).__msgs.some((m: any) => m.type === "superchat:edit-selection"),
    );
    expect((await targets(page)).map((t) => t.id)).toEqual(["cover-title"]);
    await page.close();
  });

  it("works through the real sandbox shell: nested frames, the relay and the nonce", async () => {
    if (!browser) return;
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const markup = renderToStaticMarkup(
      createElement(SandboxedHtmlViewer, {
        html: injectEditBridge(DECK, NONCE),
        title: "deck",
        relay: true,
      }),
    );
    await page.route("http://host.test/", (route) =>
      route.fulfill({ contentType: "text/html", body: `<body>${markup}</body>` }),
    );
    await page.addInitScript(() => {
      const w = window as unknown as { __msgs: unknown[] };
      w.__msgs = [];
      window.addEventListener("message", (e) => w.__msgs.push(e.data));
    });
    await page.goto("http://host.test/");
    await page.waitForFunction(() =>
      (window as unknown as { __msgs: Msg[] }).__msgs.some(
        (m) => m.type === "superchat:edit-ready",
      ),
    );
    const inner = page.frameLocator("iframe").frameLocator("iframe");
    await inner.locator('[data-superchat-id="cover-title"]').click();
    expect((await targets(page)).map((t) => t.id)).toEqual(["cover-title"]);
    // The host answers through the relay; the bridge accepts it only with the nonce.
    await page.evaluate((n) => {
      const outer = document.querySelector("iframe")?.contentWindow;
      outer?.postMessage(
        { type: "superchat:edit-select", v: 1, nonce: "wrong-wrong-wrong", ids: [] },
        "*",
      );
      outer?.postMessage(
        { type: "superchat:edit-select", v: 1, nonce: n, ids: ["cover-lead"] },
        "*",
      );
    }, NONCE);
    await page.waitForTimeout(150);
    expect((await targets(page)).map((t) => t.id)).toEqual(["cover-lead"]);
    await page.close();
  });

  it("page mode: any element is a target; one without an id is selectable, never typed into", async () => {
    if (!browser) return;
    const PAGE = `<!doctype html><html><head><title>t</title></head><body>
<main data-superchat-id="main"><form data-superchat-id="add-form">
<button type="button" data-superchat-id="add-button"><span>Add</span></button></form>
<ul></ul></main>
<script>document.querySelector("ul").innerHTML = "<li>Coffee 4.50</li>";</script>
</body></html>`;
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.addInitScript(() => {
      const w = window as unknown as { __msgs: unknown[] };
      w.__msgs = [];
      window.addEventListener("message", (e) => w.__msgs.push(e.data));
    });
    await page.route("http://page.test/", (route) =>
      route.fulfill({ contentType: "text/html", body: injectEditBridge(PAGE, NONCE, "page") }),
    );
    await page.goto("http://page.test/");
    await page.waitForFunction(() =>
      (window as unknown as { __msgs: Msg[] }).__msgs.some(
        (m) => m.type === "superchat:edit-ready",
      ),
    );
    // A click on the label's inner span selects the button around it, by its id.
    await page.click("button span");
    expect((await targets(page))[0]).toMatchObject({
      id: "add-button",
      tag: "button",
      editable: true,
      slide: 1,
    });
    expect((await targets(page))[0]?.where).toContain('form[data-superchat-id="add-form"]');
    // The list item the app drew has no id: a path id, not editable by hand.
    await page.click("li");
    const item = (await targets(page))[0];
    expect(item?.id).toMatch(/^~\d+(\.\d+)*$/);
    expect(item).toMatchObject({ tag: "li", editable: false, text: "Coffee 4.50" });
    await page.dblclick("li");
    expect(await page.getAttribute("li", "contenteditable")).toBeNull();
    // The host can select it again by that path (a new frame keeps the selection).
    await host(page, { type: "superchat:edit-select", ids: [] });
    await host(page, { type: "superchat:edit-select", ids: [item?.id] });
    expect((await targets(page)).map((t) => t.id)).toEqual([item?.id]);
    await page.close();
  });
});
