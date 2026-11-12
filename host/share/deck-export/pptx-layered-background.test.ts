// Portions modified from nexu-io/open-design apps/desktop/tests/main/pptx-layered-background.test.ts@802708f,
// Apache-2.0; changes: imports the page-side module (deck-capture-page.js) instead of
// deck-capture.ts; keeps only the fake-DOM cases of 'editable PPTX layered backgrounds' (the
// Electron-probe cases need an Electron process and are covered by the real-Chromium structural
// test in host/tests/test_deck_export.py instead).
// biome-ignore-all lint/complexity/noStaticOnlyClass: ported as written from Open Design's test
// biome-ignore-all lint/correctness/noUnsafeOptionalChaining: ported as written from Open Design's test
import { afterEach, describe, expect, test, vi } from "vitest";

import { runDomToPptx } from "./deck-capture-page.js";

class FakeStyle {
  private readonly values = new Map<string, { priority: string; value: string }>();

  getPropertyPriority(name: string): string {
    return this.values.get(name)?.priority ?? "";
  }

  getPropertyValue(name: string): string {
    return this.values.get(name)?.value ?? "";
  }

  setProperty(name: string, value: string, priority = ""): void {
    this.values.set(name, { priority, value });
  }
}

function fakeElement(tagName = "DIV"): HTMLElement {
  const attributes = new Map<string, string>();
  const children: HTMLElement[] = [];
  const element = {
    children,
    childNodes: children,
    className: "",
    closest: () => null,
    getAttribute: (name: string) => attributes.get(name) ?? null,
    innerText: "",
    offsetHeight: 80,
    offsetLeft: 24,
    offsetTop: 32,
    offsetWidth: 160,
    parentElement: null,
    append: (child: HTMLElement) => {
      (child as unknown as { parentElement: HTMLElement | null }).parentElement =
        element as unknown as HTMLElement;
      children.push(child);
    },
    insertBefore: (child: HTMLElement, before: HTMLElement) => {
      (child as unknown as { parentElement: HTMLElement | null }).parentElement =
        element as unknown as HTMLElement;
      const index = children.indexOf(before);
      if (index < 0) children.push(child);
      else children.splice(index, 0, child);
    },
    prepend: (child: HTMLElement) => {
      (child as unknown as { parentElement: HTMLElement | null }).parentElement =
        element as unknown as HTMLElement;
      children.unshift(child);
    },
    querySelectorAll: (selector: string) => {
      if (selector === ":scope > [data-od-pptx-bg]") {
        return children.filter((child) => child.getAttribute("data-od-pptx-bg") === "true");
      }
      if (selector !== "*") return [];
      const result: HTMLElement[] = [];
      const visit = (parent: HTMLElement) => {
        Array.from(parent.children).forEach((child) => {
          result.push(child as HTMLElement);
          visit(child as HTMLElement);
        });
      };
      visit(element as unknown as HTMLElement);
      return result;
    },
    remove: () => {
      const parent = element.parentElement as HTMLElement | null;
      if (!parent) return;
      const siblings = parent.children as unknown as HTMLElement[];
      const index = siblings.indexOf(element as unknown as HTMLElement);
      if (index >= 0) siblings.splice(index, 1);
      element.parentElement = null;
    },
    setAttribute: (name: string, value: string) => attributes.set(name, value),
    style: new FakeStyle(),
    tagName,
    textContent: "",
  };
  return element as unknown as HTMLElement;
}

type ComputedStyle = Record<string, string>;

function computedStyle(
  element: HTMLElement,
  overrides: Partial<ComputedStyle> = {},
): ComputedStyle {
  const inline = element.style as unknown as FakeStyle;
  const value = (name: string, fallback: string) => inline.getPropertyValue(name) || fallback;
  return {
    backgroundClip: value("background-clip", "border-box"),
    backgroundColor: value("background-color", "transparent"),
    backgroundImage: value("background-image", "none"),
    backgroundOrigin: value("background-origin", "padding-box"),
    backgroundPosition: value("background-position", "0% 0%"),
    backgroundRepeat: value("background-repeat", "repeat"),
    backgroundSize: value("background-size", "auto"),
    borderBottomWidth: "0px",
    borderLeftWidth: "0px",
    borderRadius: "0px",
    borderRightWidth: "0px",
    borderTopWidth: "0px",
    content: "none",
    display: "block",
    maskImage: "none",
    webkitMaskImage: "none",
    fontFamily: "sans-serif",
    overflow: "visible",
    paddingBottom: "0px",
    paddingLeft: "0px",
    paddingRight: "0px",
    paddingTop: "0px",
    position: value("position", "static"),
    zIndex: value("z-index", "auto"),
    ...overrides,
  };
}

function descendants(element: HTMLElement): HTMLElement[] {
  return Array.from(element.querySelectorAll<HTMLElement>("*"));
}

function stubExportDom(slide: HTMLElement, styles: Map<HTMLElement, Partial<ComputedStyle>>) {
  const body = fakeElement("BODY");
  const documentElement = fakeElement("HTML");
  const created: HTMLElement[] = [];
  const fakeDocument = {
    body,
    createElement: (tagName: string) => {
      const element = fakeElement(tagName.toUpperCase());
      created.push(element);
      return element;
    },
    createTreeWalker: () => ({ nextNode: () => null }),
    documentElement,
    querySelectorAll: (selector: string) =>
      selector === ".slide" ? [slide] : selector === "*" ? [slide, ...descendants(slide)] : [],
  };

  vi.stubGlobal("document", fakeDocument);
  vi.stubGlobal("getComputedStyle", (element: HTMLElement, pseudo?: string) =>
    pseudo
      ? computedStyle(element, { backgroundImage: "none", content: "none" })
      : computedStyle(element, styles.get(element)),
  );
  vi.stubGlobal(
    "Node",
    class FakeNode {
      static readonly TEXT_NODE = 3;
    },
  );
  vi.stubGlobal(
    "NodeFilter",
    class FakeNodeFilter {
      static readonly SHOW_TEXT = 4;
    },
  );

  return created;
}

async function runExport(
  onExport?: () => void,
  layeredBackgrounds?: Parameters<typeof runDomToPptx>[1],
): Promise<void> {
  vi.stubGlobal("window", {
    domToPptx: {
      exportToPptx: async () => {
        onExport?.();
        return new Blob(["pptx"]);
      },
    },
  });

  const result = await runDomToPptx(".slide", layeredBackgrounds);
  expect(result.error).toBeUndefined();
}

describe("editable PPTX layered backgrounds", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("isolates supported authored gradient layers for Chromium capture before native conversion", async () => {
    const slide = fakeElement();
    const paper = fakeElement();
    slide.prepend(paper);
    const backgroundImage =
      "linear-gradient(rgba(26, 26, 26, 0.018) 1px, rgba(0, 0, 0, 0) 1px), linear-gradient(rgb(248, 246, 241), rgb(248, 246, 241))";
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [slide, { position: "absolute" }],
      [
        paper,
        {
          backgroundColor: "rgb(248, 246, 241)",
          backgroundImage,
          backgroundPosition: "0% 0%, 0% 0%",
          backgroundRepeat: "repeat, repeat",
          backgroundSize: "100% 44px, 100% 100%",
          position: "absolute",
        },
      ],
    ]);
    stubExportDom(slide, styles);

    let layeredBackground: HTMLElement | undefined;
    await runExport(() => {
      layeredBackground = Array.from(paper.children).find(
        (child) => child.getAttribute("data-od-pptx-layered-bg") === "true",
      ) as HTMLElement | undefined;
    });

    expect((paper.style as unknown as FakeStyle).getPropertyValue("background-image")).toBe("none");
    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-image"),
    ).toBe(backgroundImage);
    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-color"),
    ).toBe("rgb(248, 246, 241)");
    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-size"),
    ).toBe("100% 44px, 100% 100%");
    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-repeat"),
    ).toBe("repeat, repeat");
  });

  test.each([
    "repeating-linear-gradient(90deg, red 0 8px, blue 8px 16px), linear-gradient(white, black)",
    "conic-gradient(from 45deg, red, blue), radial-gradient(white, black)",
  ])(
    "leaves html2canvas-unsupported layers on the authored element: %s",
    async (backgroundImage) => {
      const slide = fakeElement();
      const panel = fakeElement();
      slide.prepend(panel);
      const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
        [slide, { position: "absolute" }],
        [panel, { backgroundImage, position: "absolute" }],
      ]);
      const created = stubExportDom(slide, styles);

      await runExport();

      expect(
        created.filter((element) => element.tagName === "OD-PPTX-LAYERED-BACKGROUND"),
      ).toHaveLength(0);
      expect((panel.style as unknown as FakeStyle).getPropertyValue("background-image")).toBe("");
    },
  );

  test("rasterizes a static layered panel without changing its containing block semantics", async () => {
    const slide = fakeElement();
    const panel = fakeElement();
    const outerAnchoredChild = fakeElement();
    panel.prepend(outerAnchoredChild);
    slide.prepend(panel);
    const backgroundImage =
      "linear-gradient(white, transparent), radial-gradient(circle, white, black)";
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [slide, { position: "relative" }],
      [
        panel,
        {
          backgroundImage,
          position: "static",
        },
      ],
      [outerAnchoredChild, { position: "absolute" }],
    ]);
    stubExportDom(slide, styles);

    let layeredBackground: HTMLElement | undefined;
    await runExport(() => {
      layeredBackground = Array.from(panel.children).find(
        (child) => child.getAttribute("data-od-pptx-layered-bg") === "true",
      ) as HTMLElement | undefined;
    });

    expect((panel.style as unknown as FakeStyle).getPropertyValue("background-image")).toBe("none");
    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-image"),
    ).toBe(backgroundImage);
    expect((layeredBackground?.style as unknown as FakeStyle).getPropertyValue("inset")).toBe(
      "auto",
    );
    expect((layeredBackground?.style as unknown as FakeStyle).getPropertyValue("left")).toBe(
      "24px",
    );
    expect((layeredBackground?.style as unknown as FakeStyle).getPropertyValue("top")).toBe("32px");
    expect((layeredBackground?.style as unknown as FakeStyle).getPropertyValue("width")).toBe(
      "160px",
    );
    expect((layeredBackground?.style as unknown as FakeStyle).getPropertyValue("height")).toBe(
      "80px",
    );
    expect((panel.style as unknown as FakeStyle).getPropertyValue("position")).toBe("");
    expect((outerAnchoredChild.style as unknown as FakeStyle).getPropertyValue("position")).toBe(
      "",
    );
  });

  test("copies background blending onto the no-capture fallback layer", async () => {
    const slide = fakeElement();
    const panel = fakeElement();
    slide.prepend(panel);
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [slide, { position: "relative" }],
      [
        panel,
        {
          backgroundBlendMode: "multiply, screen",
          backgroundImage:
            "linear-gradient(white, transparent), radial-gradient(circle, white, black)",
          position: "absolute",
        },
      ],
    ]);
    stubExportDom(slide, styles);

    let layeredBackground: HTMLElement | undefined;
    await runExport(() => {
      layeredBackground = Array.from(panel.children).find(
        (child) => child.getAttribute("data-od-pptx-layered-bg") === "true",
      ) as HTMLElement | undefined;
    });

    expect(
      (layeredBackground?.style as unknown as FakeStyle).getPropertyValue("background-blend-mode"),
    ).toBe("multiply, screen");
  });

  test("does not turn static content wrappers into containing blocks", async () => {
    const slide = fakeElement();
    const panel = fakeElement();
    const wrapper = fakeElement();
    const outerAnchoredChild = fakeElement();
    wrapper.prepend(outerAnchoredChild);
    panel.prepend(wrapper);
    slide.prepend(panel);
    panel.setAttribute("data-od-pptx-layer-capture-id", "layer-1");
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [slide, { position: "relative" }],
      [
        panel,
        {
          backgroundImage:
            "linear-gradient(white, transparent), radial-gradient(circle, white, black)",
          position: "relative",
        },
      ],
      [wrapper, { position: "static" }],
      [outerAnchoredChild, { position: "absolute" }],
    ]);
    stubExportDom(slide, styles);

    await runExport(undefined, {
      "layer-1": {
        dataUrl: "data:image/png;base64,cG5n",
        height: 80,
        left: 24,
        slideIndex: 0,
        top: 32,
        width: 160,
      },
    });

    expect(slide.children[0]?.getAttribute("data-od-pptx-layered-bg")).toBe("true");
    expect(slide.children[1]).toBe(panel);
    expect((wrapper.style as unknown as FakeStyle).getPropertyValue("position")).toBe("");
    expect((wrapper.style as unknown as FakeStyle).getPropertyValue("z-index")).toBe("");
    expect((outerAnchoredChild.style as unknown as FakeStyle).getPropertyValue("position")).toBe(
      "",
    );
  });

  test.each([
    { clip: { backgroundClip: "text" }, name: "standard" },
    { clip: { webkitBackgroundClip: "text" }, name: "WebKit" },
  ])("keeps a $name text-clipped layered gradient on the authored text path", async ({ clip }) => {
    const slide = fakeElement();
    const title = fakeElement();
    title.textContent = "Gradient title";
    slide.prepend(title);
    const backgroundImage = "linear-gradient(90deg, red, blue), linear-gradient(white, black)";
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [slide, { position: "relative" }],
      [
        title,
        {
          backgroundImage,
          color: "transparent",
          position: "absolute",
          ...clip,
        },
      ],
    ]);
    const created = stubExportDom(slide, styles);

    let exportedBackgroundOverride = "";
    await runExport(() => {
      exportedBackgroundOverride = (title.style as unknown as FakeStyle).getPropertyValue(
        "background-image",
      );
    });

    expect(exportedBackgroundOverride).toBe("");
    expect(
      created.filter((element) => element.tagName === "OD-PPTX-LAYERED-BACKGROUND"),
    ).toHaveLength(0);
  });

  test("emits one Chromium capture layer for a slide with a supported layered background", async () => {
    const slide = fakeElement();
    const styles = new Map<HTMLElement, Partial<ComputedStyle>>([
      [
        slide,
        {
          backgroundColor: "rgb(248, 246, 241)",
          backgroundImage:
            "linear-gradient(transparent, white), radial-gradient(circle, white, transparent)",
          position: "absolute",
        },
      ],
    ]);
    const created = stubExportDom(slide, styles);

    await runExport();

    expect(
      created.filter((element) => element.tagName === "OD-PPTX-LAYERED-BACKGROUND"),
    ).toHaveLength(1);
    expect(
      created.filter((element) => element.getAttribute("data-od-pptx-bg") === "true"),
    ).toHaveLength(0);
  });
});
