// biome-ignore-all lint/complexity/useArrowFunction: extracted as written from Open Design's TypeScript; kept free of logic edits
// biome-ignore-all lint/complexity/useOptionalChain: extracted as written from Open Design's TypeScript; kept free of logic edits
// biome-ignore-all lint/suspicious/useIterableCallbackReturn: extracted as written from Open Design's TypeScript; kept free of logic edits
// Portions modified from nexu-io/open-design apps/desktop/src/main/deck-capture.ts@802708f, Apache-2.0
// (Copyright the Open Design authors; see /NOTICE). Changes: the in-page functions that Open Design
// serializes into its Electron render window (`executeJavaScript(`(${fn.toString()})(...)`)`) are
// extracted verbatim with TypeScript types stripped (no logic edits) and exported from one module,
// so the host's export helper can load them into a headless Chromium over CDP and tests can
// import them directly. Source line ranges in deck-capture.ts@802708f:
//   countRealSlides 2038-2042, showAllSlides 2048-2062, collectImportedStylesheetUrls 2064-2079,
//   cjkPromotedFontFamily 2098-2119, runDomToPptx 2128-2938, prepareDeckStage 2944-2964,
//   pinDeckStage 2969-2975, measureSlide 2981-3049, collectLayeredPptxBackgroundTargets 583-948,
//   restoreLayeredPptxBackgroundIsolation 960-973, isolateLayeredPptxBackground 975-1332.
// The Electron-only orchestration (BrowserWindow, nativeImage, the debugger attach) is replaced by
// `host/bin/superchat-deck-export` driving the same sequence over CDP. The per-slide screenshot paths of
// deck-capture.ts (renderDeckSlides image mode, showSlide, restackActiveSlide, stitching) are NOT
// ported: Superchat never exports slide images.

// Non-mutating: count the real slide surfaces (presenter clones excluded). Used
// to decide page-vs-deck BEFORE any deck-only DOM mutation, so page-mode exports
// keep the original document intact.
export function countRealSlides(slideSelector) {
  return Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb")).length;
}

// Serialized into the page: lays out every real slide simultaneously (stacked at
// the origin, opacity 1) so dom-to-pptx can measure each one as its own slide.
// Decks normally render only the active slide, which would give the others no
// layout box.
export function showAllSlides(slideSelector) {
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  for (const node of slides) {
    const el = node;
    el.style.setProperty("opacity", "1", "important");
    el.style.setProperty("visibility", "visible", "important");
    el.style.setProperty("position", "absolute", "important");
    el.style.setProperty("left", "0", "important");
    el.style.setProperty("top", "0", "important");
    ["active", "visible", "is-active", "current"].forEach((c) => el.classList.add(c));
  }
  return slides.length;
}

export function collectImportedStylesheetUrls() {
  const urls = new Set();
  const pattern = /@import\s+(?:url\(\s*)?(?:(["'])([\s\S]*?)\1|([^"')\s;]+))\s*\)?[^;]*;/giu;
  document.querySelectorAll("style").forEach((style) => {
    for (const match of (style.textContent || "").matchAll(pattern)) {
      const raw = match[2] || match[3];
      if (!raw) continue;
      try {
        urls.add(new URL(raw, document.baseURI).href);
      } catch {
        // Ignore malformed author CSS and let the normal browser fallback apply.
      }
    }
  });
  return Array.from(urls);
}

// Picks the typeface the exported PPTX should name for a run of `text`, given its
// CSS `font-family` stack. dom-to-pptx names ONE typeface per run — the first
// family in the stack — and writes it to the PowerPoint `<a:latin>`, `<a:ea>`
// (East-Asian) and `<a:cs>` slots alike. Our deck templates lead every stack
// with a Latin-only webfont (e.g. `'Inter','Noto Sans SC',…`): the browser then
// renders CJK glyphs with the later CJK family via per-glyph fallback, but the
// export mislabels those runs with the Latin font — which has no CJK glyphs — so
// PowerPoint, WPS, and Keynote each substitute a DIFFERENT fallback and the
// Chinese/Japanese/Korean text renders wrong and inconsistently ("字体错乱").
//
// When `text` contains East-Asian characters and the stack carries a CJK-capable
// family further down, return the stack reordered so that family leads (the whole
// stack is preserved so the browser keeps its own per-glyph fallback). Returns
// `null` when nothing needs to change (Latin-only text, no CJK family in the
// stack, or a CJK family already leads) so callers can skip the element. Kept
// pure and self-contained so it can be both unit-tested and serialized into the
// export render window.
export function cjkPromotedFontFamily(fontFamily, text) {
  // CJK symbols/punctuation, Hiragana, Katakana, CJK Unified Ideographs (+ Ext-A),
  // Yi, Hangul syllables, CJK compatibility ideographs, and half/fullwidth forms.
  const cjkText =
    /[\u2E80-\u2FDF\u3000-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uA000-\uA4CF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]/;
  // Family names that carry CJK glyph coverage: the Noto SC/TC/JP/KR webfonts the
  // html-ppt templates ship, plus common system CJK faces an authored deck may
  // name, so a promoted typeface resolves to a real CJK font across the office
  // suites instead of each app's arbitrary fallback.
  const cjkFamily =
    /noto\s*(sans|serif)\s*(sc|tc|hk|jp|kr|cjk)|source\s*han|pingfang|hiragino|heiti|songti|kaiti|fangsong|microsoft\s*(yahei|jhenghei)|yahei|simsun|simhei|mingliu|meiryo|ms\s*(gothic|mincho)|malgun|nanum|gulim|batang|dotum|思源|苹方|黑体|宋体|楷体|仿宋|微软雅黑|明體|明朝|ゴシック/i;
  if (!fontFamily || !cjkText.test(text || "")) return null;
  const families = fontFamily
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean);
  if (families.length < 2) return null;
  const firstCjk = families.findIndex((f) => cjkFamily.test(f.replace(/^["']|["']$/g, "").trim()));
  // No CJK family to promote, or one already leads the stack.
  if (firstCjk <= 0) return null;
  return [families[firstCjk], ...families.filter((_, i) => i !== firstCjk)].join(", ");
}

// Serialized into the page: `prepare` applies every geometry-affecting export
// normalization before Chromium capture, while `export-prepared` consumes those
// measurements without moving the DOM again. Imported font faces are exposed in
// both phases so capture uses the authored fonts and export receives the same
// explicit font list. The default phase retains the single-call test/integration
// seam. Fonts are auto-detected + embedded; SVGs stay vector (editable in
// PowerPoint).
export async function runDomToPptx(
  slideSelector,
  layeredBackgrounds = {},
  phase = "export",
  importedStylesheetOverrides = [],
) {
  // dom-to-pptx fixes native ::before content at -1,000,000. Reserve the two
  // preceding slots for its raster background and the slide background below
  // it so a slide-root pseudo remains visible over an opaque slide fill.
  const slideBackgroundSortSlot = "-1000002";
  const pseudoBeforeBackgroundSortSlot = "-1000001";
  const pseudoAfterBackgroundSortSlot = "0";
  function importedStylesheetUrls(cssText, baseHref) {
    const urls = [];
    const importPattern =
      /@import\s+(?:url\(\s*)?(?:(["'])([\s\S]*?)\1|([^"')\s;]+))\s*\)?[^;]*;/giu;
    for (const match of cssText.matchAll(importPattern)) {
      const raw = match[2] || match[3];
      if (!raw) continue;
      try {
        urls.push(new URL(raw, baseHref).href);
      } catch {
        // Ignore malformed author CSS and let the existing font fallback apply.
      }
    }
    return urls;
  }
  function importedFontFaceCss(cssText, baseHref) {
    const faces = (cssText.match(/@font-face\s*\{[\s\S]*?\}/giu) || []).map((rule) => {
      const value = (property) =>
        rule.match(new RegExp(`${property}\\s*:\\s*([^;]+)`, "iu"))?.[1]?.trim() || "";
      return {
        family: value("font-family").replace(/^['"]|['"]$/g, ""),
        rule,
        style: value("font-style").toLowerCase() || "normal",
        unicodeRange: value("unicode-range"),
        weight: value("font-weight").toLowerCase() || "400",
      };
    });
    const preferredFace = new Map();
    for (const face of faces) {
      const rank =
        face.style === "normal" ? (face.weight === "400" || face.weight === "normal" ? 0 : 1) : 2;
      const current = preferredFace.get(face.family);
      if (!current || rank < current.rank) {
        preferredFace.set(face.family, { rank, style: face.style, weight: face.weight });
      }
    }
    const preferredRule = new Map();
    for (const face of faces) {
      const preferred = preferredFace.get(face.family);
      if (preferred?.style !== face.style || preferred.weight !== face.weight) continue;
      // Google Fonts commonly returns one @font-face per unicode subset. The
      // vendored converter can fail while merging some families' subsets, so
      // prefer the complete face when present, then its Latin core subset.
      const rank = face.unicodeRange === "" ? 0 : /U\+0000-00FF/iu.test(face.unicodeRange) ? 1 : 2;
      const current = preferredRule.get(face.family);
      if (!current || rank < current.rank)
        preferredRule.set(face.family, { rank, rule: face.rule });
    }
    return faces
      .filter((face) => preferredRule.get(face.family)?.rule === face.rule)
      .map((rule) =>
        rule.rule.replace(/url\(\s*(["']?)([^"')]+)\1\s*\)/giu, (_match, _quote, raw) => {
          try {
            return `url("${new URL(raw.trim(), baseHref).href}")`;
          } catch {
            return `url("${raw.trim()}")`;
          }
        }),
      )
      .join("\n");
  }
  // dom-to-pptx's autoEmbedFonts scanner sees top-level CSSFontFaceRule entries,
  // but many OpenDesign decks load Google Fonts through an inline `@import`.
  // Expand those imports into a throwaway top-level style so the vendored engine
  // can discover and embed the actual font files instead of only writing their
  // family names into the PPTX. The render window is destroyed after export, so
  // this never mutates the authored HTML or the live preview.
  async function exposeImportedFontFaces() {
    const importedUrls = new Set();
    document.querySelectorAll("style").forEach((style) => {
      for (const url of importedStylesheetUrls(style.textContent || "", document.baseURI)) {
        importedUrls.add(url);
      }
    });
    if (importedUrls.size === 0) return [];
    const visited = new Set();
    const fontFaceRules = [];
    const collect = async (url) => {
      if (visited.has(url)) return;
      visited.add(url);
      try {
        const override = importedStylesheetOverrides.find((entry) => entry.url === url);
        const response = override ? null : await fetch(url);
        if (response && !response.ok) throw new Error(`HTTP ${response.status}`);
        const cssText = override?.cssText ?? (await response.text());
        for (const nested of importedStylesheetUrls(cssText, url)) await collect(nested);
        const fontCss = importedFontFaceCss(cssText, url);
        if (fontCss) fontFaceRules.push(fontCss);
      } catch (error) {
        console.warn("Cannot expose imported fonts for editable PPTX:", url, error);
      }
    };
    for (const url of importedUrls) await collect(url);
    if (fontFaceRules.length === 0) return [];
    const combinedCss = fontFaceRules.join("\n");
    const style = document.createElement("style");
    style.setAttribute("data-od-pptx-imported-font-faces", "true");
    style.textContent = combinedCss;
    document.head.appendChild(style);
    const fontsByFamily = new Map();
    for (const rule of combinedCss.match(/@font-face\s*\{[\s\S]*?\}/giu) || []) {
      const family = rule
        .match(/font-family\s*:\s*([^;]+)/iu)?.[1]
        ?.trim()
        .replace(/^['"]|['"]$/g, "");
      if (!family) continue;
      const urls = fontsByFamily.get(family) || new Set();
      for (const match of rule.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/giu)) {
        if (match[1]) urls.add(match[1]);
      }
      if (urls.size > 0) fontsByFamily.set(family, urls);
    }
    return Array.from(fontsByFamily, ([name, urls]) => ({ name, urls: Array.from(urls) }));
  }
  function isTransparentColor(input) {
    const value = input.trim().toLowerCase();
    return value === "" || value === "transparent" || value === "rgba(0, 0, 0, 0)";
  }
  function firstCssColor(input) {
    const rgb = input.match(/rgba?\([^)]*\)/i);
    if (rgb) return rgb[0];
    const hex = input.match(/#[0-9a-f]{3,8}\b/i);
    return hex ? hex[0] : null;
  }
  function effectiveBackgroundStyle(slide) {
    const candidates = [];
    for (let el = slide; el; el = el.parentElement) candidates.push(el);
    if (document.body && !candidates.includes(document.body)) candidates.push(document.body);
    if (document.documentElement && !candidates.includes(document.documentElement)) {
      candidates.push(document.documentElement);
    }
    for (const el of candidates) {
      const style = getComputedStyle(el);
      const bgColor = style.backgroundColor;
      const bgImage = style.backgroundImage;
      const hasImage = bgImage && bgImage !== "none";
      const hasColor = bgColor && !isTransparentColor(bgColor);
      const fallbackColor = hasColor ? bgColor : firstCssColor(bgImage);
      if (!hasImage && !hasColor) continue;
      if (!fallbackColor) continue;
      return {
        color: fallbackColor,
        image: bgImage,
        position: style.backgroundPosition,
        size: style.backgroundSize,
        repeat: style.backgroundRepeat,
        origin: style.backgroundOrigin,
        clip: style.backgroundClip,
      };
    }
    return null;
  }
  function ensureExplicitSlideBackgrounds(slides) {
    for (const slide of slides) {
      slide.querySelectorAll(":scope > [data-od-pptx-bg]").forEach((el) => el.remove());
      // preserveLayeredGradientBackgrounds owns supported layered backgrounds
      // authored directly on a slide. Adding the usual fallback shim as well
      // would export the same semi-transparent texture twice.
      if (hasRasterizableLayeredGradientBackground(getComputedStyle(slide).backgroundImage || "")) {
        continue;
      }
      const background = effectiveBackgroundStyle(slide);
      if (!background) continue;
      const bg = document.createElement("div");
      bg.setAttribute("data-od-pptx-bg", "true");
      bg.setAttribute("aria-hidden", "true");
      bg.style.setProperty("position", "absolute", "important");
      bg.style.setProperty("inset", "0", "important");
      bg.style.setProperty("z-index", slideBackgroundSortSlot, "important");
      bg.style.setProperty("pointer-events", "none", "important");
      bg.style.setProperty("background-color", background.color, "important");
      bg.style.setProperty("background-image", background.image, "important");
      bg.style.setProperty("background-position", background.position, "important");
      bg.style.setProperty("background-size", background.size, "important");
      bg.style.setProperty("background-repeat", background.repeat, "important");
      bg.style.setProperty("background-origin", background.origin, "important");
      bg.style.setProperty("background-clip", background.clip, "important");
      const style = getComputedStyle(slide);
      if (style.position === "static") slide.style.setProperty("position", "relative", "important");
      if (style.overflow === "visible") slide.style.setProperty("overflow", "hidden", "important");
      slide.style.setProperty("background-color", background.color, "important");
      Array.from(slide.children).forEach((child) => {
        if (child.getAttribute("data-od-pptx-bg") === "true") return;
        const childStyle = getComputedStyle(child);
        const element = child;
        if (childStyle.position === "static") {
          element.style.setProperty("position", "relative", "important");
        }
        if (childStyle.zIndex === "auto") {
          element.style.setProperty("z-index", "1", "important");
        }
      });
      slide.prepend(bg);
    }
  }
  function splitCssBackgroundLayers(input) {
    const layers = [];
    let current = "";
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (const char of input) {
      if (escaped) {
        current += char;
        escaped = false;
        continue;
      }
      if (char === "\\") {
        current += char;
        escaped = true;
        continue;
      }
      if (quote) {
        current += char;
        if (char === quote) quote = "";
        continue;
      }
      if (char === '"' || char === "'") {
        current += char;
        quote = char;
        continue;
      }
      if (char === "(") depth += 1;
      else if (char === ")") depth = Math.max(0, depth - 1);
      if (char === "," && depth === 0) {
        if (current.trim()) layers.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    if (current.trim()) layers.push(current.trim());
    return layers;
  }
  function hasRasterizableLayeredGradientBackground(input) {
    const layers = splitCssBackgroundLayers(input);
    if (layers.length < 2) return false;
    // Keep this allowlist aligned with html2canvas 1.4.1's
    // SUPPORTED_IMAGE_FUNCTIONS. In particular, repeating and conic gradients
    // are discarded by its clone parser and must remain on the authored node.
    const supportedGradient =
      /^(?:(?:-(?:moz|ms|o|webkit)-)?(?:linear|radial)-gradient|-webkit-gradient)\(/i;
    return layers.every((layer) => supportedGradient.test(layer));
  }
  function hasTextBackgroundClip(input) {
    return splitCssBackgroundLayers(input).some((layer) => layer.toLowerCase() === "text");
  }
  function hasNonNormalBlendMode(input) {
    const mode = (input || "normal").trim().toLowerCase();
    return mode !== "" && mode !== "normal";
  }
  function hasBackdropFilter(style) {
    const value = (
      style.backdropFilter ||
      style.getPropertyValue?.("backdrop-filter") ||
      style.getPropertyValue?.("-webkit-backdrop-filter") ||
      "none"
    )
      .trim()
      .toLowerCase();
    return value !== "" && value !== "none";
  }
  function hasCssMask(style) {
    const maskImages = [
      style.maskImage || style.getPropertyValue("mask-image"),
      style.webkitMaskImage || style.getPropertyValue("-webkit-mask-image"),
    ];
    return maskImages.some((image) => image && image.trim().toLowerCase() !== "none");
  }
  function setCaptureBoxStyles(background, style) {
    background.style.setProperty("box-sizing", "border-box", "important");
    background.style.setProperty(
      "padding",
      `${style.paddingTop || "0px"} ${style.paddingRight || "0px"} ${style.paddingBottom || "0px"} ${style.paddingLeft || "0px"}`,
      "important",
    );
    background.style.setProperty(
      "border-width",
      `${style.borderTopWidth || "0px"} ${style.borderRightWidth || "0px"} ${style.borderBottomWidth || "0px"} ${style.borderLeftWidth || "0px"}`,
      "important",
    );
    background.style.setProperty("border-style", "solid", "important");
    background.style.setProperty("border-color", "transparent", "important");
    background.style.setProperty("border-radius", style.borderRadius || "0px", "important");
    background.style.setProperty("box-shadow", style.boxShadow || "none", "important");
    background.style.setProperty("background-color", style.backgroundColor, "important");
    background.style.setProperty("background-image", style.backgroundImage, "important");
    background.style.setProperty("background-position", style.backgroundPosition, "important");
    background.style.setProperty("background-size", style.backgroundSize, "important");
    background.style.setProperty("background-repeat", style.backgroundRepeat, "important");
    background.style.setProperty("background-origin", style.backgroundOrigin, "important");
    background.style.setProperty("background-clip", style.backgroundClip, "important");
    background.style.setProperty(
      "background-blend-mode",
      style.backgroundBlendMode || "normal",
      "important",
    );
    background.style.setProperty("clip-path", style.clipPath || "none", "important");
    background.style.setProperty("filter", style.filter || "none", "important");
    const backdropFilter =
      style.backdropFilter ||
      style.getPropertyValue?.("backdrop-filter") ||
      style.getPropertyValue?.("-webkit-backdrop-filter") ||
      "none";
    background.style.setProperty("backdrop-filter", backdropFilter, "important");
    background.style.setProperty("-webkit-backdrop-filter", backdropFilter, "important");
    background.style.setProperty("opacity", style.opacity || "1", "important");
    background.style.setProperty("mix-blend-mode", style.mixBlendMode || "normal", "important");
    background.style.setProperty("transform", style.transform || "none", "important");
    background.style.setProperty(
      "transform-origin",
      style.transformOrigin || "50% 50%",
      "important",
    );
    background.style.setProperty("transform-box", style.transformBox || "view-box", "important");
    background.style.setProperty("translate", style.translate || "none", "important");
    background.style.setProperty("rotate", style.rotate || "none", "important");
    background.style.setProperty("scale", style.scale || "none", "important");
  }
  function preserveLayeredPseudoGradientBackgrounds(elements) {
    let nativePseudoBackgroundStyle = null;
    const neutralizeNativePseudoBackground = (element, pseudo) => {
      element.setAttribute(
        pseudo === "::before"
          ? "data-od-pptx-rasterized-before-background"
          : "data-od-pptx-rasterized-after-background",
        "true",
      );
      if (nativePseudoBackgroundStyle) return;
      nativePseudoBackgroundStyle = document.createElement("style");
      nativePseudoBackgroundStyle.textContent = `
        [data-od-pptx-rasterized-before-background="true"]::before,
        [data-od-pptx-rasterized-after-background="true"]::after{
          background-color:transparent!important;
        }
      `;
      document.head.append(nativePseudoBackgroundStyle);
    };
    for (const element of elements) {
      for (const pseudo of ["::before", "::after"]) {
        const style = getComputedStyle(element, pseudo);
        const content = (style.content || "").trim().toLowerCase();
        const isGenerated =
          content !== "" && content !== "none" && content !== "normal" && style.display !== "none";
        const hasMaterializedCapture = Array.from(element.children).some(
          (child) => child.getAttribute("data-od-pptx-materialized-pseudo") === pseudo,
        );
        if (hasMaterializedCapture) {
          // The Chromium helper already owns the computed fallback color. Keep
          // native pseudo text and borders, but prevent dom-to-pptx from
          // emitting that same color as an opaque fill above the captured PNG.
          neutralizeNativePseudoBackground(element, pseudo);
          continue;
        }
        if (
          !isGenerated ||
          (style.position !== "absolute" && style.position !== "fixed") ||
          !hasRasterizableLayeredGradientBackground(style.backgroundImage || "") ||
          // The html2canvas custom-element path has no blend-mode parser and
          // cannot reproduce this background without its authored backdrop.
          hasNonNormalBlendMode(style.mixBlendMode || "") ||
          hasBackdropFilter(style) ||
          hasTextBackgroundClip(style.backgroundClip || "") ||
          hasTextBackgroundClip(style.webkitBackgroundClip || "") ||
          hasCssMask(style)
        ) {
          continue;
        }
        // dom-to-pptx only reads pseudo-element content, color, and border. A
        // background-only custom element enters its existing html2canvas path,
        // preserving the layered image while the native pseudo handling keeps
        // any authored text or border editable.
        const background = document.createElement("od-pptx-layered-background");
        background.setAttribute("data-od-pptx-layered-bg", "true");
        background.setAttribute("data-od-pptx-pseudo", pseudo);
        background.setAttribute("aria-hidden", "true");
        background.style.setProperty("position", style.position, "important");
        background.style.setProperty("top", style.top || "auto", "important");
        background.style.setProperty("right", style.right || "auto", "important");
        background.style.setProperty("bottom", style.bottom || "auto", "important");
        background.style.setProperty("left", style.left || "auto", "important");
        background.style.setProperty("width", style.width || "auto", "important");
        background.style.setProperty("height", style.height || "auto", "important");
        // Keep the raster background immediately below the converter's fixed
        // native pseudo text/border slots. Native ::after always sorts at the
        // host's z=0 Infinity slot, regardless of its authored z-index.
        background.style.setProperty(
          "z-index",
          pseudo === "::before" ? pseudoBeforeBackgroundSortSlot : pseudoAfterBackgroundSortSlot,
          "important",
        );
        background.style.setProperty("pointer-events", "none", "important");
        setCaptureBoxStyles(background, style);
        // The converter keeps pseudo content and borders editable, but it also
        // emits a native solid fill from background-color while ignoring the
        // layered background-image. The raster helper already owns both, so
        // neutralize only that native fallback after copying its computed color.
        neutralizeNativePseudoBackground(element, pseudo);
        if (pseudo === "::before") element.prepend(background);
        else element.append(background);
      }
    }
  }
  function suppressCapturedSlidePaint(slide, capture) {
    // dom-to-pptx needs the slide itself to remain measurable as the export
    // root. Keep only the replacement image visible inside it and neutralize
    // effects that Chromium already baked into that whole-paint capture.
    slide.querySelectorAll("*").forEach((descendant) => {
      if (descendant !== capture && !capture.contains(descendant)) {
        descendant.style.setProperty("display", "none", "important");
      }
    });
    slide.style.setProperty("background", "transparent", "important");
    slide.style.setProperty("border", "0", "important");
    slide.style.setProperty("box-shadow", "none", "important");
    slide.style.setProperty("clip-path", "none", "important");
    slide.style.setProperty("color", "transparent", "important");
    slide.style.setProperty("filter", "none", "important");
    slide.style.setProperty("backdrop-filter", "none", "important");
    slide.style.setProperty("-webkit-backdrop-filter", "none", "important");
    slide.style.setProperty("mask-image", "none", "important");
    slide.style.setProperty("-webkit-mask-image", "none", "important");
    slide.style.setProperty("mix-blend-mode", "normal", "important");
    slide.style.setProperty("opacity", "1", "important");
    slide.style.setProperty("outline", "none", "important");
    slide.style.setProperty("text-shadow", "none", "important");
    slide.style.setProperty("-webkit-text-fill-color", "transparent", "important");
    slide.style.setProperty("transform", "none", "important");
    slide.style.setProperty("translate", "none", "important");
    slide.style.setProperty("rotate", "none", "important");
    slide.style.setProperty("scale", "none", "important");
  }
  function preserveLayeredGradientBackgrounds(slides) {
    if (
      document.querySelectorAll("[data-od-pptx-suppress-before], [data-od-pptx-suppress-after]")
        .length > 0
    ) {
      const suppressedPseudoStyle = document.createElement("style");
      suppressedPseudoStyle.textContent = `
        [data-od-pptx-suppress-before="true"]::before,
        [data-od-pptx-suppress-after="true"]::after{
          content:none!important;
          display:none!important;
          border:0!important;
          background:none!important;
        }
      `;
      document.head.append(suppressedPseudoStyle);
    }
    const slideElements = new Set(slides);
    const elements = new Set();
    for (const slide of slides) {
      elements.add(slide);
      slide.querySelectorAll("*").forEach((el) => elements.add(el));
    }
    const capturedCompositingMembers = new Set();
    const capturedEntirePaintRoots = new Set();
    for (const element of elements) {
      if (element.getAttribute("data-od-pptx-compositing-context") !== "true") continue;
      const captureId = element.getAttribute("data-od-pptx-layer-capture-id") || "";
      const captured = layeredBackgrounds[captureId];
      if (!captured) continue;
      const slide = slides[captured.slideIndex];
      if (!slide) continue;
      const style = getComputedStyle(element);
      // Export the flattened context beside its source. Ordinary members lose
      // only the backgrounds already present in the PNG; members whose own
      // compositor effect required whole-paint capture are suppressed entirely.
      const image = document.createElement("img");
      image.setAttribute("data-od-pptx-layered-bg", "true");
      image.setAttribute("aria-hidden", "true");
      image.src = captured.dataUrl;
      image.style.setProperty("position", "absolute", "important");
      image.style.setProperty("left", `${captured.left}px`, "important");
      image.style.setProperty("top", `${captured.top}px`, "important");
      image.style.setProperty("width", `${captured.width}px`, "important");
      image.style.setProperty("height", `${captured.height}px`, "important");
      image.style.setProperty("display", "block", "important");
      image.style.setProperty("object-fit", "fill", "important");
      image.style.setProperty("pointer-events", "none", "important");
      image.style.setProperty("z-index", style.zIndex || "auto", "important");
      image.getBoundingClientRect = () => {
        const slideRect = slide.getBoundingClientRect();
        const left = slideRect.left + captured.left;
        const top = slideRect.top + captured.top;
        return {
          bottom: top + captured.height,
          height: captured.height,
          left,
          right: left + captured.width,
          top,
          width: captured.width,
          x: left,
          y: top,
          toJSON: () => ({}),
        };
      };
      if (element === slide) slide.prepend(image);
      else element.parentElement?.insertBefore(image, element);
      document
        .querySelectorAll(`[data-od-pptx-compositing-member="${captureId}"]`)
        .forEach((member) => {
          if (
            member.hasAttribute("data-od-pptx-materialized-pseudo") ||
            member.hasAttribute("data-od-pptx-capture-entire-element")
          ) {
            if (member === slide) suppressCapturedSlidePaint(member, image);
            else member.style.setProperty("display", "none", "important");
            capturedEntirePaintRoots.add(member);
          } else {
            member.style.setProperty("background-image", "none", "important");
            member.style.setProperty("background-color", "transparent", "important");
          }
          capturedCompositingMembers.add(member);
        });
    }
    for (const element of elements) {
      if (capturedCompositingMembers.has(element)) continue;
      if (Array.from(capturedEntirePaintRoots).some((root) => root.contains(element))) continue;
      const style = getComputedStyle(element);
      const captureId = element.getAttribute("data-od-pptx-layer-capture-id") || "";
      const captured = layeredBackgrounds[captureId];
      if (
        !hasRasterizableLayeredGradientBackground(style.backgroundImage || "") ||
        (!captured &&
          (hasTextBackgroundClip(style.backgroundClip || "") ||
            hasTextBackgroundClip(style.webkitBackgroundClip || ""))) ||
        // The custom-element fallback uses html2canvas, which cannot preserve
        // masks. Production exports provide a Chromium capture for these.
        (hasCssMask(style) && !captured)
      ) {
        continue;
      }
      const isStaticNestedElement = style.position === "static" && !slideElements.has(element);
      if (captured) {
        const slide = slides[captured.slideIndex];
        if (!slide) continue;
        const materializedPseudo = element.getAttribute("data-od-pptx-materialized-pseudo");
        const capturesEntireElement =
          element.getAttribute("data-od-pptx-capture-entire-element") === "true";
        const background = document.createElement("img");
        background.setAttribute("data-od-pptx-layered-bg", "true");
        if (materializedPseudo) background.setAttribute("data-od-pptx-pseudo", materializedPseudo);
        background.setAttribute("aria-hidden", "true");
        background.src = captured.dataUrl;
        background.style.setProperty("position", "absolute", "important");
        background.style.setProperty("left", `${captured.left}px`, "important");
        background.style.setProperty("top", `${captured.top}px`, "important");
        background.style.setProperty("width", `${captured.width}px`, "important");
        background.style.setProperty("height", `${captured.height}px`, "important");
        background.style.setProperty("display", "block", "important");
        background.style.setProperty("object-fit", "fill", "important");
        background.style.setProperty("pointer-events", "none", "important");
        background.style.setProperty(
          "z-index",
          element === slide
            ? slideBackgroundSortSlot
            : materializedPseudo === "::before"
              ? pseudoBeforeBackgroundSortSlot
              : materializedPseudo === "::after"
                ? pseudoAfterBackgroundSortSlot
                : style.zIndex || "auto",
          "important",
        );
        background.getBoundingClientRect = () => {
          const slideRect = slide.getBoundingClientRect();
          const left = slideRect.left + captured.left;
          const top = slideRect.top + captured.top;
          return {
            bottom: top + captured.height,
            height: captured.height,
            left,
            right: left + captured.width,
            top,
            width: captured.width,
            x: left,
            y: top,
            toJSON: () => ({}),
          };
        };
        element.style.setProperty("background-image", "none", "important");
        element.style.setProperty("background-color", "transparent", "important");
        if (element === slide) slide.prepend(background);
        else element.parentElement?.insertBefore(background, element);
        if (materializedPseudo || capturesEntireElement) {
          // The helper exists only to give Chromium a real capture target. Its
          // raster image now owns that paint; leaving the custom element in the
          // converter walk would emit the same pseudo as a second media layer.
          if (element === slide) suppressCapturedSlidePaint(element, background);
          else element.style.setProperty("display", "none", "important");
          capturedEntirePaintRoots.add(element);
        }
        continue;
      }
      // dom-to-pptx's native gradient parser assumes one linear-gradient and
      // greedily merges layered gradients into one invalid SVG. In test-only
      // callers without the main-process capture seam, retain the existing
      // custom-element fallback for unmasked layers.
      const background = document.createElement("od-pptx-layered-background");
      background.setAttribute("data-od-pptx-layered-bg", "true");
      background.setAttribute("aria-hidden", "true");
      background.style.setProperty("position", "absolute", "important");
      background.style.setProperty("inset", "0", "important");
      background.style.setProperty(
        "z-index",
        slideElements.has(element) ? slideBackgroundSortSlot : "0",
        "important",
      );
      background.style.setProperty("pointer-events", "none", "important");
      setCaptureBoxStyles(background, style);
      if (isStaticNestedElement) {
        // A static panel and its absolutely positioned descendants share the
        // same outer containing block. Anchor only the capture child to the
        // panel's measured border box so the authored panel never becomes a
        // new containing block.
        background.style.setProperty("inset", "auto", "important");
        background.style.setProperty("left", `${element.offsetLeft}px`, "important");
        background.style.setProperty("top", `${element.offsetTop}px`, "important");
        background.style.setProperty("width", `${element.offsetWidth}px`, "important");
        background.style.setProperty("height", `${element.offsetHeight}px`, "important");
      } else {
        // ensureExplicitSlideBackgrounds already establishes this containing-
        // block contract for slides; positioned authored elements already own
        // the absolutely positioned capture child.
        if (style.position === "static")
          element.style.setProperty("position", "relative", "important");
      }
      element.style.setProperty("background-image", "none", "important");
      element.style.setProperty("background-color", "transparent", "important");
      element.prepend(background);
    }
    preserveLayeredPseudoGradientBackgrounds(elements);
  }
  function stabilizeLargeSingleLineText(slides) {
    for (const slide of slides) {
      slide.querySelectorAll("*").forEach((el) => {
        const rawText = el.innerText || el.textContent || "";
        const text = rawText.replace(/\s+/g, " ").trim();
        if (!text || rawText.includes("\n")) return;
        const style = getComputedStyle(el);
        const fontSizePx = Number.parseFloat(style.fontSize);
        if (!Number.isFinite(fontSizePx) || fontSizePx < 96) return;
        const lineHeightPx = Number.parseFloat(style.lineHeight);
        if (!Number.isFinite(lineHeightPx) || lineHeightPx <= 0 || lineHeightPx > fontSizePx * 1.05)
          return;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 1 || rect.height <= 1) return;
        const justify =
          style.textAlign === "center" || style.textAlign === "-webkit-center"
            ? "center"
            : style.textAlign === "right" || style.textAlign === "end"
              ? "flex-end"
              : "flex-start";
        el.style.setProperty("display", "flex", "important");
        el.style.setProperty("align-items", "center", "important");
        el.style.setProperty("justify-content", justify, "important");
        el.style.setProperty("width", `${rect.width}px`, "important");
        el.style.setProperty("height", `${rect.height}px`, "important");
        el.style.setProperty("line-height", "normal", "important");
        el.style.setProperty("white-space", "nowrap", "important");
        el.style.setProperty("overflow", "visible", "important");
      });
    }
  }
  // An authored `<br>` is a deliberate line boundary. Prevent PowerPoint/WPS
  // from applying a second soft wrap inside either line when its font metrics
  // differ slightly from Chromium's. dom-to-pptx maps `white-space: nowrap` to
  // `wrap: false` while retaining explicit breakLine runs.
  function stabilizeAuthoredHeadingLines(slides) {
    for (const slide of slides) {
      slide.querySelectorAll("h1, h2, h3").forEach((heading) => {
        if (heading.querySelector("br")) {
          heading.style.setProperty("white-space", "nowrap", "important");
        }
      });
    }
  }
  // Reorder each text run's font-family so CJK runs name their CJK typeface (not
  // the Latin webfont that leads our template stacks) before dom-to-pptx reads it,
  // so PowerPoint/WPS/Keynote all resolve the same real font. See
  // cjkPromotedFontFamily for the why. Keyed on the element that directly owns the
  // text so a container that only holds Latin markup is never rewritten. Decide on
  // the element's COMBINED direct text: bilingual markup often splits one element
  // across text nodes (`Product Launch<br>产品发布`, `Welcome <strong>…</strong> 欢迎`),
  // so a later CJK chunk must still win even when a Latin chunk comes first.
  function promoteCjkTypefaces(slides) {
    const touched = new Set();
    for (const slide of slides) {
      const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node.parentElement;
        if (!el || touched.has(el)) continue;
        touched.add(el);
        let combined = "";
        for (const child of el.childNodes) {
          if (child.nodeType === Node.TEXT_NODE) combined += child.nodeValue || "";
        }
        if (!combined.trim()) continue;
        const promoted = cjkPromotedFontFamily(getComputedStyle(el).fontFamily, combined);
        if (promoted) el.style.setProperty("font-family", promoted, "important");
      }
    }
  }
  try {
    const w = window;
    if (!w.domToPptx || typeof w.domToPptx.exportToPptx !== "function") {
      return { error: "dom-to-pptx engine did not load" };
    }
    const slides = Array.prototype.slice
      .call(document.querySelectorAll(slideSelector))
      .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
    if (slides.length === 0) return { error: "no slides to export" };
    const importedFonts = await exposeImportedFontFaces();
    await document.fonts?.ready;
    if (phase !== "export-prepared") {
      // Superchat addition: Chart.js charts become native PowerPoint charts (never a canvas picture).
      markNativeCharts(slideSelector);
      ensureExplicitSlideBackgrounds(slides);
      stabilizeLargeSingleLineText(slides);
      stabilizeAuthoredHeadingLines(slides);
      promoteCjkTypefaces(slides);
      // dom-to-pptx assumes `node.className` is a string, but SVG elements expose
      // an SVGAnimatedString, so its DOM walk throws on decks containing inline SVG.
      // Normalize those to a plain string in this throwaway render window.
      document.querySelectorAll("*").forEach((el) => {
        const cn = el.className;
        if (cn != null && typeof cn !== "string") {
          try {
            Object.defineProperty(el, "className", {
              value: cn.baseVal ?? "",
              configurable: true,
              writable: true,
            });
          } catch {
            // Leave it; dom-to-pptx may still handle this node.
          }
        }
      });
    }
    if (phase === "prepare") return { prepared: true };
    preserveLayeredGradientBackgrounds(slides);
    const blob = await w.domToPptx.exportToPptx(slides, {
      fileName: "deck.pptx",
      skipDownload: true,
      autoEmbedFonts: true,
      ...(importedFonts.length > 0 ? { fonts: importedFonts } : {}),
      svgAsVector: true,
    });
    if (!blob || typeof blob.arrayBuffer !== "function") {
      return { error: "dom-to-pptx returned no blob" };
    }
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)));
    }
    return { b64: btoa(binary) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

// Deck-only DOM prep (run only once we've decided this is a deck): hide presenter
// chrome, switch any <deck-stage> runtime to authored (1:1) size, and freeze
// animations/transitions so each slide (and its reveal-on-show inner elements,
// e.g. `.slide.visible .reveal`) reaches its final state.
export function prepareDeckStage(hideSelector, stageSelector) {
  document.querySelectorAll(hideSelector).forEach((el) => {
    el.style.setProperty("display", "none", "important");
  });
  // The repo's <deck-stage> runtime fits its canvas to the viewport with
  // `transform: scale(...)` by default and documents that export must set the
  // `noscale` attribute so the DOM is captured at the authored slide size. Set
  // it here (no-op for plain `.slide` decks that have no <deck-stage>), or a
  // deck whose authored canvas differs from the 1920x1080 capture viewport would
  // be measured + captured at the preview-scaled size instead of 1:1.
  document.querySelectorAll(stageSelector).forEach((el) => {
    el.setAttribute("noscale", "");
    const style = el.style;
    style.setProperty("transform", "none", "important");
    style.setProperty("transform-origin", "top left", "important");
  });
  const s = document.createElement("style");
  s.textContent =
    "*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition-duration:0s!important;transition-delay:0s!important}";
  (document.head || document.documentElement).appendChild(s);
}

// Deck-only: pin to the measured WxH stage so each slide captures
// deterministically. NOT applied in page mode — an ordinary page must keep its
// natural width/height.
export function pinDeckStage(w, h, stageSelector) {
  const style = document.createElement("style");
  style.textContent =
    `html,body{margin:0!important;padding:0!important;width:${w}px!important;height:${h}px!important;overflow:hidden!important}` +
    `.deck,${stageSelector}{width:${w}px!important;height:${h}px!important}`;
  document.head.appendChild(style);
}

// Serialized into the page: measures the authored slide box. Prefers a slide
// that already has a non-zero layout rect (covers decks that hide inactive
// slides via opacity/visibility); if every slide is display:none, force-measures
// the first one off-screen. Returns the authored DIP size or null.
export function measureSlide(slideSelector, stageSelector) {
  function positiveCssNumber(value) {
    if (typeof value === "number") return Number.isFinite(value) && value > 1 ? value : null;
    if (typeof value !== "string") return null;
    const trimmed = value.trim();
    const match = /^(\d+(?:\.\d+)?)(?:px)?$/i.exec(trimmed);
    if (!match) return null;
    const n = Number(match[1]);
    return Number.isFinite(n) && n > 1 ? n : null;
  }
  function sizePair(w, h) {
    const width = positiveCssNumber(w);
    const height = positiveCssNumber(h);
    return width != null && height != null ? { w: width, h: height } : null;
  }
  function deckStageAuthoredSize(stage) {
    const byProp = sizePair(stage.designWidth, stage.designHeight);
    if (byProp) return byProp;
    const byAttr = sizePair(stage.getAttribute("width"), stage.getAttribute("height"));
    if (byAttr) return byAttr;
    const byStyle = sizePair(stage.style?.width, stage.style?.height);
    if (byStyle) return byStyle;
    const computed = window.getComputedStyle?.(stage);
    const byComputed = computed ? sizePair(computed.width, computed.height) : null;
    if (byComputed) return byComputed;
    return sizePair(stage.offsetWidth, stage.offsetHeight);
  }
  function measureAuthored(el) {
    const stage = el.closest(stageSelector);
    const stageSize = stage ? deckStageAuthoredSize(stage) : null;
    if (stageSize) return stageSize;
    const attrSize = sizePair(el.getAttribute("width"), el.getAttribute("height"));
    if (attrSize) return attrSize;
    const styleSize = sizePair(el.style?.width, el.style?.height);
    if (styleSize) return styleSize;
    const computed = window.getComputedStyle?.(el);
    const computedSize = computed ? sizePair(computed.width, computed.height) : null;
    if (computedSize) return computedSize;
    const offsetSize = sizePair(el.offsetWidth, el.offsetHeight);
    if (offsetSize) return offsetSize;
    return null;
  }
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  if (slides.length === 0) return null;
  for (const node of slides) {
    const authored = measureAuthored(node);
    if (authored) return authored;
    const r = node.getBoundingClientRect();
    if (r.width > 1 && r.height > 1) return { w: r.width, h: r.height };
  }
  const el = slides[0];
  const prev = el.style.cssText;
  el.style.setProperty("display", "block", "important");
  el.style.setProperty("visibility", "hidden", "important");
  const authored = measureAuthored(el);
  if (authored) {
    el.style.cssText = prev;
    return authored;
  }
  const rect = el.getBoundingClientRect();
  el.style.cssText = prev;
  return rect.width > 1 && rect.height > 1 ? { w: rect.width, h: rect.height } : null;
}

export function collectLayeredPptxBackgroundTargets(slideSelector) {
  function splitLayers(input) {
    const layers = [];
    let current = "";
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (const char of input) {
      if (escaped) {
        current += char;
        escaped = false;
        continue;
      }
      if (char === "\\") {
        current += char;
        escaped = true;
        continue;
      }
      if (quote) {
        current += char;
        if (char === quote) quote = "";
        continue;
      }
      if (char === '"' || char === "'") {
        current += char;
        quote = char;
        continue;
      }
      if (char === "(") depth += 1;
      else if (char === ")") depth = Math.max(0, depth - 1);
      if (char === "," && depth === 0) {
        if (current.trim()) layers.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    if (current.trim()) layers.push(current.trim());
    return layers;
  }
  function isSupportedLayeredGradient(input) {
    const layers = splitLayers(input);
    if (layers.length < 2) return false;
    const supportedGradient =
      /^(?:(?:-(?:moz|ms|o|webkit)-)?(?:linear|radial)-gradient|-webkit-gradient)\(/i;
    return layers.every((layer) => supportedGradient.test(layer));
  }
  function hasTextClip(style) {
    return [style.backgroundClip, style.webkitBackgroundClip]
      .flatMap((value) => splitLayers(value || ""))
      .some((value) => value.toLowerCase() === "text");
  }
  function hasNonNormalBlendMode(style) {
    const mode = (style.mixBlendMode || "normal").trim().toLowerCase();
    return mode !== "" && mode !== "normal";
  }
  function hasBackdropFilter(style) {
    const value = (
      style.backdropFilter ||
      style.getPropertyValue?.("backdrop-filter") ||
      style.getPropertyValue?.("-webkit-backdrop-filter") ||
      "none"
    )
      .trim()
      .toLowerCase();
    return value !== "" && value !== "none";
  }
  function dependsOnBackdrop(style) {
    return hasNonNormalBlendMode(style) || hasBackdropFilter(style);
  }
  function hasNonNormalBackgroundBlendMode(style) {
    return (style.backgroundBlendMode || "normal")
      .split(",")
      .some((mode) => mode.trim().toLowerCase() !== "normal");
  }
  function hasCssMask(style) {
    const images = [
      style.maskImage || style.getPropertyValue("mask-image"),
      style.webkitMaskImage || style.getPropertyValue("-webkit-mask-image"),
    ];
    return images.some((image) => image && image.trim().toLowerCase() !== "none");
  }
  function hasCssClipPath(style) {
    const value = (
      style.clipPath ||
      style.getPropertyValue?.("clip-path") ||
      style.getPropertyValue?.("-webkit-clip-path") ||
      "none"
    )
      .trim()
      .toLowerCase();
    return value !== "" && value !== "none";
  }
  function copyComputedMaskStyles(background, style) {
    for (let index = 0; index < style.length; index += 1) {
      const property = style.item(index);
      if (!property.startsWith("mask-") && !property.startsWith("-webkit-mask-")) continue;
      const value = style.getPropertyValue(property);
      if (value) background.style.setProperty(property, value, "important");
    }
  }
  function hasUnsupportedPseudoSelfEffect(style) {
    const opacity = Number.parseFloat(style.opacity || "1");
    const hasOpacity = Number.isFinite(opacity) && opacity > 0 && opacity < 1;
    return hasOpacity || hasCssClipPath(style) || hasUnsupportedNativeAncestorEffect(style);
  }
  function materializeLayeredPseudoBackground(element, pseudo) {
    const style = getComputedStyle(element, pseudo);
    const rawContent = (style.content || "").trim();
    const content = rawContent.toLowerCase();
    const isGenerated =
      content !== "" && content !== "none" && content !== "normal" && style.display !== "none";
    const materializeEntirePseudo =
      hasTextClip(style) || hasCssMask(style) || hasUnsupportedPseudoSelfEffect(style);
    if (
      !isGenerated ||
      (style.position !== "absolute" && style.position !== "fixed") ||
      !isSupportedLayeredGradient(style.backgroundImage || "") ||
      (!materializeEntirePseudo && !hasNonNormalBackgroundBlendMode(style))
    ) {
      return null;
    }
    // Materialize pseudo backgrounds whose compositing is not faithfully
    // represented by the html2canvas fallback. Chromium retains internal
    // background blending, and backdrop-dependent pseudos additionally mark
    // the authored backdrop for flattening into the same PNG.
    const background = document.createElement("od-pptx-layered-background");
    background.setAttribute("data-od-pptx-materialized-pseudo", pseudo);
    if (materializeEntirePseudo) {
      background.setAttribute("data-od-pptx-materialized-entire-pseudo", "true");
      element.setAttribute(
        pseudo === "::before" ? "data-od-pptx-suppress-before" : "data-od-pptx-suppress-after",
        "true",
      );
    }
    if (dependsOnBackdrop(style)) {
      background.setAttribute("data-od-pptx-flatten-blend-backdrop", "true");
    }
    background.setAttribute("aria-hidden", "true");
    background.style.setProperty("position", style.position, "important");
    background.style.setProperty("top", style.top || "auto", "important");
    background.style.setProperty("right", style.right || "auto", "important");
    background.style.setProperty("bottom", style.bottom || "auto", "important");
    background.style.setProperty("left", style.left || "auto", "important");
    background.style.setProperty("width", style.width || "auto", "important");
    background.style.setProperty("height", style.height || "auto", "important");
    background.style.setProperty("box-sizing", "border-box", "important");
    background.style.setProperty(
      "padding",
      `${style.paddingTop || "0px"} ${style.paddingRight || "0px"} ${style.paddingBottom || "0px"} ${style.paddingLeft || "0px"}`,
      "important",
    );
    background.style.setProperty(
      "border-width",
      `${style.borderTopWidth || "0px"} ${style.borderRightWidth || "0px"} ${style.borderBottomWidth || "0px"} ${style.borderLeftWidth || "0px"}`,
      "important",
    );
    background.style.setProperty("border-style", "solid", "important");
    background.style.setProperty("border-color", "transparent", "important");
    background.style.setProperty("border-radius", style.borderRadius || "0px", "important");
    background.style.setProperty("box-shadow", style.boxShadow || "none", "important");
    background.style.setProperty("background-color", style.backgroundColor, "important");
    background.style.setProperty("background-image", style.backgroundImage, "important");
    background.style.setProperty("background-position", style.backgroundPosition, "important");
    background.style.setProperty("background-size", style.backgroundSize, "important");
    background.style.setProperty("background-repeat", style.backgroundRepeat, "important");
    background.style.setProperty("background-origin", style.backgroundOrigin, "important");
    background.style.setProperty("background-clip", style.backgroundClip, "important");
    background.style.setProperty(
      "background-blend-mode",
      style.backgroundBlendMode || "normal",
      "important",
    );
    background.style.setProperty("clip-path", style.clipPath || "none", "important");
    copyComputedMaskStyles(background, style);
    background.style.setProperty("filter", style.filter || "none", "important");
    const backdropFilter =
      style.backdropFilter ||
      style.getPropertyValue?.("backdrop-filter") ||
      style.getPropertyValue?.("-webkit-backdrop-filter") ||
      "none";
    background.style.setProperty("backdrop-filter", backdropFilter, "important");
    background.style.setProperty("-webkit-backdrop-filter", backdropFilter, "important");
    background.style.setProperty("opacity", style.opacity || "1", "important");
    background.style.setProperty("mix-blend-mode", style.mixBlendMode, "important");
    background.style.setProperty("transform", style.transform || "none", "important");
    background.style.setProperty(
      "transform-origin",
      style.transformOrigin || "50% 50%",
      "important",
    );
    background.style.setProperty("transform-box", style.transformBox || "view-box", "important");
    background.style.setProperty("translate", style.translate || "none", "important");
    background.style.setProperty("rotate", style.rotate || "none", "important");
    background.style.setProperty("scale", style.scale || "none", "important");
    background.style.setProperty("z-index", style.zIndex || "auto", "important");
    if (materializeEntirePseudo) {
      // Text clipping, masks, and non-serializable self effects apply to the
      // pseudo's complete painted output, including generated text and borders.
      // Copy the computed pseudo style so Chromium rasterizes it as one layer.
      for (let index = 0; index < style.length; index += 1) {
        const property = style.item(index);
        if (property === "content") continue;
        const value = style.getPropertyValue(property);
        if (value) background.style.setProperty(property, value, "important");
      }
      background.textContent = rawContent.replace(/^['"]|['"]$/g, "");
    }
    background.style.setProperty("pointer-events", "none", "important");
    if (pseudo === "::before") element.prepend(background);
    else element.append(background);
    return background;
  }
  function isRenderedInsideSlide(element, slide, style) {
    for (let current = element; current; current = current.parentElement) {
      const currentStyle = current === element ? style : getComputedStyle(current);
      const visibility = currentStyle.visibility.toLowerCase();
      if (
        currentStyle.display === "none" ||
        visibility === "hidden" ||
        visibility === "collapse" ||
        Number.parseFloat(currentStyle.opacity) === 0
      ) {
        return false;
      }
      if (current === slide) break;
    }
    const targetRect = element.getBoundingClientRect();
    if (targetRect.width < 1 || targetRect.height < 1) return false;
    const slideRect = slide.getBoundingClientRect();
    const hasVisualOverflow = style.filter && style.filter !== "none";
    const padding = hasVisualOverflow
      ? Math.min(192, Math.max(64, Math.ceil(Math.max(targetRect.width, targetRect.height) / 4)))
      : 0;
    const width =
      Math.min(slideRect.right, targetRect.right + padding) -
      Math.max(slideRect.left, targetRect.left - padding);
    const height =
      Math.min(slideRect.bottom, targetRect.bottom + padding) -
      Math.max(slideRect.top, targetRect.top - padding);
    return width >= 1 && height >= 1;
  }
  function establishesCompositingContext(style) {
    const opacity = Number.parseFloat(style.opacity || "1");
    const hasOpacity = Number.isFinite(opacity) && opacity > 0 && opacity < 1;
    return hasOpacity || hasUnsupportedNativeAncestorEffect(style);
  }
  function hasUnsupportedNativeAncestorEffect(style) {
    const hasFilter = Boolean(style.filter && style.filter !== "none");
    const hasTransform = [style.transform, style.translate, style.rotate, style.scale].some(
      (value) => Boolean(value && value !== "none"),
    );
    return (
      hasFilter ||
      hasTransform ||
      hasCssClipPath(style) ||
      hasCssMask(style) ||
      dependsOnBackdrop(style)
    );
  }
  function compositingAncestors(element, slide) {
    const ancestors = [];
    let compositingBoundary = 0;
    for (
      let ancestor = element.parentElement;
      ancestor && ancestor !== slide;
      ancestor = ancestor.parentElement
    ) {
      ancestors.push(ancestor);
      if (establishesCompositingContext(getComputedStyle(ancestor))) {
        compositingBoundary = ancestors.length;
      }
    }
    if (element !== slide && hasUnsupportedNativeAncestorEffect(getComputedStyle(slide))) {
      ancestors.push(slide);
      compositingBoundary = ancestors.length;
    }
    // A painted wrapper between the layered target and the outer compositor
    // participates in the same group even when it establishes no compositor
    // of its own. Keep the complete chain through the outermost boundary so
    // its fill is captured and cannot be re-emitted above the replacement PNG.
    return ancestors.slice(0, compositingBoundary);
  }
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((element) => !element.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  const targets = [];
  let nextId = 0;
  for (const slide of slides) {
    const authoredElements = [slide, ...Array.from(slide.querySelectorAll("*"))];
    const materializedPseudos = authoredElements.flatMap((element) =>
      ["::before", "::after"]
        .map((pseudo) => materializeLayeredPseudoBackground(element, pseudo))
        .filter((element) => element !== null),
    );
    const elements = [...authoredElements, ...materializedPseudos];
    const layeredElements = elements.filter((element) => {
      const style = getComputedStyle(element);
      const capturesLayer =
        isSupportedLayeredGradient(style.backgroundImage || "") &&
        isRenderedInsideSlide(element, slide, style);
      if (
        capturesLayer &&
        (hasTextClip(style) || hasCssMask(style) || establishesCompositingContext(style)) &&
        !element.hasAttribute("data-od-pptx-materialized-pseudo")
      ) {
        // The native converter cannot reapply text clipping, masks, or
        // compositor effects to a background raster and editable foreground as
        // one CSS paint group. Keep the complete element paint in one Chromium
        // capture instead.
        element.setAttribute("data-od-pptx-capture-entire-element", "true");
        element.setAttribute("data-od-pptx-suppress-before", "true");
        element.setAttribute("data-od-pptx-suppress-after", "true");
      }
      return capturesLayer;
    });
    const captureGroups = new Map();
    for (const element of layeredElements) {
      // PPTX has no equivalent of a DOM ancestor compositing group here. When
      // an ancestor effect encloses layered descendants, capture that outermost
      // context once instead of baking the effect into each background image.
      // Every intermediate compositor participates in that same flattened paint
      // and must have its native background cleared after capture as well.
      const ancestors = compositingAncestors(element, slide);
      for (const ancestor of ancestors) {
        if (!hasUnsupportedNativeAncestorEffect(getComputedStyle(ancestor))) continue;
        // dom-to-pptx can inherit ancestor opacity, but it cannot serialize an
        // ancestor filter, transform, blend mode, or backdrop filter onto the
        // editable foreground. Capture and suppress that affected subtree so
        // Chromium applies the unsupported effect exactly once.
        ancestor.setAttribute("data-od-pptx-capture-entire-element", "true");
        ancestor.setAttribute("data-od-pptx-suppress-before", "true");
        ancestor.setAttribute("data-od-pptx-suppress-after", "true");
      }
      const root = ancestors.at(-1) ?? element;
      const members = captureGroups.get(root) ?? [];
      for (const member of [element, ...ancestors]) {
        if (!members.includes(member)) members.push(member);
      }
      captureGroups.set(root, members);
    }
    for (const [element, members] of captureGroups) {
      const style = getComputedStyle(element);
      if (!isRenderedInsideSlide(element, slide, style)) continue;
      const id = `od-pptx-layer-${nextId++}`;
      if (members.some((member) => member !== element)) {
        // The compositor root's own background participates in the same paint
        // group even when it is a solid fill rather than a layered gradient.
        // Capture and clear it with the layered descendants so its later
        // native fill cannot cover the flattened PNG.
        const compositingMembers = members.includes(element) ? members : [element, ...members];
        element.setAttribute("data-od-pptx-compositing-context", "true");
        for (const member of compositingMembers) {
          member.setAttribute("data-od-pptx-compositing-member", id);
        }
      }
      if (
        dependsOnBackdrop(style) ||
        members.some((member) => dependsOnBackdrop(getComputedStyle(member)))
      ) {
        element.setAttribute("data-od-pptx-flatten-blend-backdrop", "true");
      }
      element.setAttribute("data-od-pptx-layer-capture-id", id);
      targets.push({ id });
    }
  }
  return targets;
}

export function isolateLayeredPptxBackground(slideSelector, id) {
  restoreLayeredPptxBackgroundIsolation();
  const target = document.querySelector(`[data-od-pptx-layer-capture-id="${id}"]`);
  if (!target) return null;
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((element) => !element.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  const slideIndex = slides.findIndex((slide) => slide === target || slide.contains(target));
  if (slideIndex < 0) return null;
  const slide = slides[slideIndex];
  const targetStyle = getComputedStyle(target);
  const flattenBackdrop = target.getAttribute("data-od-pptx-flatten-blend-backdrop") === "true";
  const flattenCompositingContext =
    target.getAttribute("data-od-pptx-compositing-context") === "true";
  const materializesEntirePseudo =
    target.getAttribute("data-od-pptx-materialized-entire-pseudo") === "true";
  const capturesEntireElement =
    target.getAttribute("data-od-pptx-capture-entire-element") === "true";
  const targetRect = target.getBoundingClientRect();
  const slideRect = slide.getBoundingClientRect();
  const hasVisualOverflow = targetStyle.filter && targetStyle.filter !== "none";
  const padding = hasVisualOverflow
    ? Math.min(192, Math.max(64, Math.ceil(Math.max(targetRect.width, targetRect.height) / 4)))
    : 0;
  const left = Math.max(slideRect.left, targetRect.left - padding);
  const top = Math.max(slideRect.top, targetRect.top - padding);
  const right = Math.min(slideRect.right, targetRect.right + padding);
  const bottom = Math.min(slideRect.bottom, targetRect.bottom + padding);
  if (right - left < 1 || bottom - top < 1) return null;
  const allElements = Array.from(document.querySelectorAll("*"));
  const inlineStyles = allElements.map((element) => ({ cssText: element.style.cssText, element }));
  const compositingMembers = new Set(
    flattenCompositingContext
      ? Array.from(document.querySelectorAll(`[data-od-pptx-compositing-member="${id}"]`))
      : [],
  );
  const backdropDependentMembers = Array.from(compositingMembers).filter((element) => {
    const style = getComputedStyle(element);
    const mixBlendMode = (style.mixBlendMode || "normal").trim().toLowerCase();
    const backdropFilter = (
      style.backdropFilter ||
      style.getPropertyValue?.("backdrop-filter") ||
      style.getPropertyValue?.("-webkit-backdrop-filter") ||
      "none"
    )
      .trim()
      .toLowerCase();
    return (
      (mixBlendMode !== "" && mixBlendMode !== "normal") ||
      (backdropFilter !== "" && backdropFilter !== "none")
    );
  });
  const backdropPaintTargets =
    backdropDependentMembers.length > 0 ? backdropDependentMembers : [target];
  const entirePaintRoots = new Set(
    flattenCompositingContext
      ? Array.from(compositingMembers).filter(
          (element) =>
            element.hasAttribute("data-od-pptx-capture-entire-element") ||
            element.hasAttribute("data-od-pptx-materialized-entire-pseudo"),
        )
      : capturesEntireElement || materializesEntirePseudo
        ? [target]
        : [],
  );
  const entirePaintStyles = new Map();
  for (const root of entirePaintRoots) {
    for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
      const style = getComputedStyle(element);
      entirePaintStyles.set(element, {
        color: style.color,
        textFillColor: style.getPropertyValue("-webkit-text-fill-color") || style.color,
        textShadow: style.textShadow || "none",
        visibility: style.visibility,
      });
    }
  }
  const blendBackdropElements = new Set();
  const blendBackdropPseudos = new Map();
  const addBlendBackdropElement = (element) => {
    blendBackdropElements.add(element);
    const pseudos = blendBackdropPseudos.get(element) ?? new Set();
    pseudos.add("::before");
    pseudos.add("::after");
    blendBackdropPseudos.set(element, pseudos);
  };
  const paintsBehindTarget = (element) => {
    const rect = element.getBoundingClientRect();
    const intersection = {
      bottom: Math.min(bottom, rect.bottom),
      left: Math.max(left, rect.left),
      right: Math.min(right, rect.right),
      top: Math.max(top, rect.top),
    };
    if (intersection.right - intersection.left < 1 || intersection.bottom - intersection.top < 1) {
      return false;
    }
    const insetX = Math.min(1, (intersection.right - intersection.left) / 4);
    const insetY = Math.min(1, (intersection.bottom - intersection.top) / 4);
    const points = [
      [(intersection.left + intersection.right) / 2, (intersection.top + intersection.bottom) / 2],
      [intersection.left + insetX, intersection.top + insetY],
      [intersection.right - insetX, intersection.top + insetY],
      [intersection.left + insetX, intersection.bottom - insetY],
      [intersection.right - insetX, intersection.bottom - insetY],
    ];
    let sampledTogether = false;
    const paintsBehindAt = (x, y) => {
      const paintStack = document.elementsFromPoint(x, y);
      const elementIndex = paintStack.indexOf(element);
      if (elementIndex < 0) return false;
      return backdropPaintTargets.some((paintTarget) => {
        const targetIndex = paintStack.indexOf(paintTarget);
        if (targetIndex >= 0) sampledTogether = true;
        return targetIndex >= 0 && elementIndex > targetIndex;
      });
    };
    if (points.some(([x, y]) => paintsBehindAt(x, y))) return true;
    const paintClipState = (candidate) => {
      const style = getComputedStyle(candidate);
      const clipPath = (
        style.clipPath ||
        style.getPropertyValue?.("clip-path") ||
        style.getPropertyValue?.("-webkit-clip-path") ||
        "none"
      )
        .trim()
        .toLowerCase();
      const hasMask = [
        style.maskImage || style.getPropertyValue?.("mask-image"),
        style.webkitMaskImage || style.getPropertyValue?.("-webkit-mask-image"),
      ].some((value) => Boolean(value && value.trim().toLowerCase() !== "none"));
      return { hasClipPath: clipPath !== "" && clipPath !== "none", hasMask };
    };
    const hasPaintClip = (candidate) => {
      const state = paintClipState(candidate);
      return state.hasClipPath || state.hasMask;
    };
    const paintClipChain = (candidate) => {
      const clips = [];
      for (let current = candidate; current; current = current.parentElement) {
        if (hasPaintClip(current)) clips.push(current);
        if (current === slide) break;
      }
      return clips;
    };
    const clippedPaintBoxes = [
      ...paintClipChain(element),
      ...backdropPaintTargets.flatMap((paintTarget) => paintClipChain(paintTarget)),
    ].filter((candidate, index, candidates) => candidates.indexOf(candidate) === index);
    if (clippedPaintBoxes.length === 0) return false;
    // A clip path or mask on either box or one of its ancestors can confine
    // real paint to a narrow stripe or ring that misses every fixed sample.
    // Expand those clips for the paint-order probe without removing them:
    // clip-path and masks establish stacking contexts, so setting them to
    // `none` can reorder the boxes and invert the result we are trying to
    // measure. The authored styles are restored before capture, where the real
    // clip/mask still shapes the PNG.
    const paintClipStyles = clippedPaintBoxes.map((candidate) => ({
      candidate,
      cssText: candidate.style.cssText,
      ...paintClipState(candidate),
    }));
    try {
      for (const { candidate, hasClipPath, hasMask } of paintClipStyles) {
        if (hasClipPath) {
          candidate.style.setProperty("clip-path", "inset(0)", "important");
          candidate.style.setProperty("-webkit-clip-path", "inset(0)", "important");
        }
        if (hasMask) {
          for (const property of ["mask", "-webkit-mask"]) {
            candidate.style.setProperty(property, "linear-gradient(#000, #000)", "important");
          }
        }
      }
      sampledTogether = false;
      if (points.some(([x, y]) => paintsBehindAt(x, y))) return true;
      if (sampledTogether) return false;
      // Nested clipping can still keep the two boxes out of Chromium's sampled
      // stack. Preserve the possible backdrop rather than dropping authored
      // paint when coverage remains uncertain.
      return true;
    } finally {
      for (const { candidate, cssText } of paintClipStyles) {
        candidate.style.cssText = cssText;
      }
    }
  };
  if (flattenBackdrop) {
    // Hit testing is Chromium's public view of the effective paint stack. Make
    // pointer-events:none export shims participate temporarily, then use their
    // actual order rather than assuming that DOM order is paint order.
    for (const element of allElements) {
      element.style.setProperty("pointer-events", "auto", "important");
    }
    for (const element of [slide, ...Array.from(slide.querySelectorAll("*"))]) {
      const isCapturedDescendant =
        target.contains(element) && (!flattenCompositingContext || compositingMembers.has(element));
      if (element === target || element.contains(target) || isCapturedDescendant) continue;
      if (paintsBehindTarget(element)) addBlendBackdropElement(element);
    }
    const materializedPseudo = target.getAttribute("data-od-pptx-materialized-pseudo");
    for (let branch = target; branch && branch !== slide; branch = branch.parentElement) {
      const parent = branch.parentElement;
      if (!parent) break;
      blendBackdropElements.add(parent);
      // An ancestor's ::before paints before its child content and is part of
      // that child's blend backdrop. For a materialized ::before target, the
      // immediate parent's pseudo is the source layer itself, not its backdrop.
      if (!(branch === target && materializedPseudo === "::before")) {
        const pseudos = blendBackdropPseudos.get(parent) ?? new Set();
        pseudos.add("::before");
        blendBackdropPseudos.set(parent, pseudos);
      }
    }
  }
  const pseudoBackdropAttributes = Array.from(blendBackdropPseudos, ([element, pseudos]) => ({
    after: element.getAttribute("data-od-pptx-blend-backdrop-after"),
    before: element.getAttribute("data-od-pptx-blend-backdrop-before"),
    element,
    pseudos,
  }));
  for (const { element, pseudos } of pseudoBackdropAttributes) {
    if (pseudos.has("::before")) element.setAttribute("data-od-pptx-blend-backdrop-before", id);
    if (pseudos.has("::after")) element.setAttribute("data-od-pptx-blend-backdrop-after", id);
  }
  const pseudoStyle = document.createElement("style");
  const entireElementPseudoScope = flattenCompositingContext
    ? `[data-od-pptx-compositing-member="${id}"][data-od-pptx-capture-entire-element="true"]`
    : `[data-od-pptx-layer-capture-id="${id}"][data-od-pptx-capture-entire-element="true"]`;
  pseudoStyle.textContent = `
    *::before,*::after{visibility:hidden!important}
    [data-od-pptx-blend-backdrop-before="${id}"]::before,
    [data-od-pptx-blend-backdrop-after="${id}"]::after{
      visibility:visible!important;
      color:transparent!important;
      text-shadow:none!important;
      -webkit-text-fill-color:transparent!important;
    }
    ${
      capturesEntireElement ||
      Array.from(entirePaintRoots).some((element) =>
        element.hasAttribute("data-od-pptx-capture-entire-element"),
      )
        ? `
      ${entireElementPseudoScope}::before,
      ${entireElementPseudoScope}::after,
      ${entireElementPseudoScope} *::before,
      ${entireElementPseudoScope} *::after{
        visibility:visible!important;
      }
    `
        : ""
    }
  `;
  document.head.append(pseudoStyle);
  window.__odPptxLayerIsolation = {
    inlineStyles,
    pseudoBackdropAttributes,
    pseudoStyle,
  };
  for (const element of allElements) element.style.setProperty("visibility", "hidden", "important");
  for (const element of blendBackdropElements) {
    element.style.setProperty("visibility", "visible", "important");
    element.style.setProperty("color", "transparent", "important");
    element.style.setProperty("outline", "none", "important");
    element.style.setProperty("text-shadow", "none", "important");
    element.style.setProperty("-webkit-text-fill-color", "transparent", "important");
  }
  for (let ancestor = target; ancestor; ancestor = ancestor.parentElement) {
    ancestor.style.setProperty("visibility", "visible", "important");
    if (ancestor !== target) {
      if (!flattenBackdrop) {
        ancestor.style.setProperty("background", "transparent", "important");
        ancestor.style.setProperty("border-color", "transparent", "important");
        ancestor.style.setProperty("box-shadow", "none", "important");
      }
      ancestor.style.setProperty("color", "transparent", "important");
      ancestor.style.setProperty("outline", "none", "important");
      ancestor.style.setProperty("text-shadow", "none", "important");
    }
  }
  for (const descendant of Array.from(target.querySelectorAll("*"))) {
    const entirePaintRoot = Array.from(entirePaintRoots).find(
      (root) => root === descendant || root.contains(descendant),
    );
    if (entirePaintRoot) {
      descendant.style.setProperty(
        "visibility",
        entirePaintStyles.get(descendant)?.visibility || "visible",
        "important",
      );
      continue;
    }
    if (blendBackdropElements.has(descendant)) continue;
    if (!flattenCompositingContext) {
      descendant.style.setProperty("visibility", "hidden", "important");
      continue;
    }
    const carriesCapturedBackground = compositingMembers.has(descendant);
    const containsCapturedBackground = Array.from(compositingMembers).some((member) =>
      descendant.contains(member),
    );
    if (!carriesCapturedBackground && !containsCapturedBackground) {
      descendant.style.setProperty("visibility", "hidden", "important");
      continue;
    }
    descendant.style.setProperty("visibility", "visible", "important");
    if (!carriesCapturedBackground) {
      descendant.style.setProperty("background", "transparent", "important");
    }
    descendant.style.setProperty("border-color", "transparent", "important");
    descendant.style.setProperty("box-shadow", "none", "important");
    descendant.style.setProperty("color", "transparent", "important");
    descendant.style.setProperty("outline", "none", "important");
    descendant.style.setProperty("text-shadow", "none", "important");
    descendant.style.setProperty("-webkit-text-fill-color", "transparent", "important");
  }
  // Replaced content is painted by the target itself rather than a descendant.
  // Move that object outside its content box for this screenshot while leaving
  // the target's CSS background visible; restore replays the saved inline style.
  if (!capturesEntireElement && ["IMG", "CANVAS", "VIDEO"].includes(target.tagName)) {
    target.style.setProperty("object-position", "1000000px 1000000px", "important");
  }
  if (flattenCompositingContext && !entirePaintRoots.has(target)) {
    if (!compositingMembers.has(target)) {
      target.style.setProperty("background", "transparent", "important");
    }
    target.style.setProperty("border-color", "transparent", "important");
    target.style.setProperty("box-shadow", "none", "important");
    target.style.setProperty("color", "transparent", "important");
    target.style.setProperty("outline", "none", "important");
    target.style.setProperty("text-shadow", "none", "important");
    target.style.setProperty("-webkit-text-fill-color", "transparent", "important");
  } else if (!materializesEntirePseudo && !capturesEntireElement) {
    target.style.setProperty("border-color", "transparent", "important");
    target.style.setProperty("box-shadow", "none", "important");
    target.style.setProperty("color", "transparent", "important");
    target.style.setProperty("text-shadow", "none", "important");
    target.style.setProperty("-webkit-text-fill-color", "transparent", "important");
  }
  for (const [element, style] of entirePaintStyles) {
    element.style.setProperty("visibility", style.visibility, "important");
    element.style.setProperty("color", style.color, "important");
    element.style.setProperty("text-shadow", style.textShadow, "important");
    element.style.setProperty("-webkit-text-fill-color", style.textFillColor, "important");
  }
  for (const root of [document.documentElement, document.body]) {
    if (!root) continue;
    root.style.setProperty("visibility", "visible", "important");
    if (!flattenBackdrop) root.style.setProperty("background", "transparent", "important");
  }
  return {
    height: bottom - top,
    left: left - slideRect.left,
    pageX: left + window.scrollX,
    pageY: top + window.scrollY,
    slideIndex,
    top: top - slideRect.top,
    width: right - left,
  };
}

export function restoreLayeredPptxBackgroundIsolation() {
  const state = window.__odPptxLayerIsolation;
  if (!state) return;
  for (const { cssText, element } of state.inlineStyles) element.style.cssText = cssText;
  for (const { after, before, element } of state.pseudoBackdropAttributes) {
    if (before === null) element.removeAttribute("data-od-pptx-blend-backdrop-before");
    else element.setAttribute("data-od-pptx-blend-backdrop-before", before);
    if (after === null) element.removeAttribute("data-od-pptx-blend-backdrop-after");
    else element.setAttribute("data-od-pptx-blend-backdrop-after", after);
  }
  state.pseudoStyle.remove();
  delete window.__odPptxLayerIsolation;
}

// ---------------------------------------------------------------------------------------------
// Portions modified from nexu-io/open-design apps/desktop/src/main/pdf-export.ts@802708f, Apache-2.0.
// Changes: DECK_PRINT_CSS is verbatim; `waitForPrintableContentInPage` is the executeJavaScript
// body of waitForPrintableContent with the budget as an argument (the Electron-side
// webContents.stop() on give-up is done by the helper with Page.stopLoading);
// `unhideDeckSlidesForPrint` is the executeJavaScript body of the same-named function.
// ---------------------------------------------------------------------------------------------

export const DECK_PRINT_CSS = `
@media print {
  @page { size: 1920px 1080px; margin: 0; }
  html, body {
    width: 1920px !important;
    height: auto !important;
    overflow: visible !important;
    background: #fff !important;
  }
  body {
    display: block !important;
    scroll-snap-type: none !important;
    transform: none !important;
  }
  .slide, [data-screen-label], section.slide, .deck-slide, .ppt-slide {
    flex: none !important;
    width: 1920px !important;
    height: 1080px !important;
    min-height: 1080px !important;
    max-height: 1080px !important;
    page-break-after: always;
    break-after: page;
    scroll-snap-align: none !important;
    transform: none !important;
    position: relative !important;
    overflow: hidden !important;
    /* Decks commonly show one slide at a time via opacity; without this the
       inactive slides print as blank pages. */
    opacity: 1 !important;
    visibility: visible !important;
    animation: none !important;
    transition: none !important;
  }
  .slide:last-child, [data-screen-label]:last-child { page-break-after: auto; break-after: auto; }
  .deck-counter, .deck-hint, .deck-nav,
  [aria-label="Previous slide"], [aria-label="Next slide"] {
    display: none !important;
  }
}
`;

export function unhideDeckSlidesForPrint() {
  document
    .querySelectorAll(".slide, [data-screen-label], .deck-slide, .ppt-slide")
    .forEach(function (el) {
      ["active", "visible", "is-active", "current"].forEach(function (c) {
        el.classList.add(c);
      });
    });
}

export function waitForPrintableContentInPage(budgetMs) {
  var RESOURCE_TIMEOUT_MS = Math.max(1000, Math.round((budgetMs * 2) / 3));

  var stalledCount = 0;
  function withDeadline(promise) {
    return Promise.race([
      promise,
      new Promise(function (resolve) {
        setTimeout(function () {
          stalledCount += 1;
          resolve();
        }, RESOURCE_TIMEOUT_MS);
      }),
    ]);
  }

  function waitForImages() {
    return Promise.all(
      Array.from(document.images || []).map(function (img) {
        if (img.complete) return Promise.resolve();
        return withDeadline(
          new Promise(function (resolve) {
            img.addEventListener("load", resolve, { once: true });
            img.addEventListener("error", resolve, { once: true });
          }),
        );
      }),
    );
  }

  function cssUrlValues(value) {
    var urls = [];
    if (!value || value === "none") return urls;
    value.replace(/url\((['"]?)(.*?)\1\)/g, function (_, _quote, rawUrl) {
      if (rawUrl && !/^data:/i.test(rawUrl)) urls.push(rawUrl);
      return "";
    });
    return urls;
  }

  function waitForCssBackgroundImages() {
    var urls = new Set();
    Array.from(document.querySelectorAll("*")).forEach(function (el) {
      var style = window.getComputedStyle(el);
      cssUrlValues(style.backgroundImage).forEach(function (url) {
        urls.add(url);
      });
      cssUrlValues(style.borderImageSource).forEach(function (url) {
        urls.add(url);
      });
      cssUrlValues(style.listStyleImage).forEach(function (url) {
        urls.add(url);
      });
    });
    return Promise.all(
      Array.from(urls).map(function (url) {
        return withDeadline(
          new Promise(function (resolve) {
            var img = new Image();
            img.onload = resolve;
            img.onerror = resolve;
            img.src = url;
          }),
        );
      }),
    );
  }

  function nextFrame() {
    return new Promise(function (resolve) {
      requestAnimationFrame(function () {
        resolve(true);
      });
    });
  }

  return Promise.all([
    document.fonts && document.fonts.ready
      ? withDeadline(document.fonts.ready.catch(function () {}))
      : Promise.resolve(),
    waitForImages(),
    waitForCssBackgroundImages(),
  ])
    .then(nextFrame)
    .then(nextFrame)
    .then(function () {
      return { stalled: stalledCount > 0 };
    });
}

// ---------------------------------------------------------------------------------------------
// Superchat additions (original).
// ---------------------------------------------------------------------------------------------

/** The slide-surface family and real-slide filter used by every Open Design step above. */
export const SLIDE_SELECTOR = ".slide, [data-screen-label], .deck-slide, .ppt-slide";
export const DECK_STAGE_SELECTOR = "deck-stage, #deck-stage, .deck-stage";
export const HIDE_CHROME_SELECTOR =
  ".progress-bar, .notes-overlay, aside.notes, .speaker-notes, .deck-nav, .deck-hint, .deck-counter";

/** Two animation frames, like deck-capture.ts `nextFrames`. */
export function nextFrames() {
  return new Promise(function (r) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        r(true);
      });
    });
  });
}

/**
 * Whether a PNG (base64) has any visible pixel. Port of deck-capture.ts `pngBufferHasPaint`
 * (alpha > 0 anywhere), decoded in the page because the helper has no image library.
 */
export async function pngHasPaint(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0);
  const data = ctx.getImageData(0, 0, bitmap.width, bitmap.height).data;
  for (let offset = 3; offset < data.length; offset += 4) {
    if (data[offset] > 0) return true;
  }
  return false;
}

/**
 * Authoring lint for a Superchat deck, run in the same laid-out render the exporter uses (every slide
 * stacked at the origin, unscaled). It reports what would make the PowerPoint differ from the
 * page, so authoring can fix it before export: content outside the slide or inside the footer
 * rail, clipped text, text objects without a stable data-superchat-id, duplicate ids, unembedded
 * fonts, unreadable sizes, long headlines, and effects that can only become a picture layer.
 * Pure measurement: nothing is changed in the document.
 *
 * @param {{ slideSelector: string, railTop: number }} options `railTop` is the slide-relative y
 *   where the footer rail starts (content must end above it).
 * @returns {Array<{slide: number, label: string, id: string, severity: string, code: string, message: string}>}
 */
export function lintDeck(options) {
  const slideSelector = options.slideSelector;
  const railTop = options.railTop;
  const issues = [];
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  const embedded = new Set();
  document.fonts.forEach((face) =>
    embedded.add(
      String(face.family)
        .replace(/^["']|["']$/g, "")
        .toLowerCase(),
    ),
  );
  const systemOk = new Set([
    "sans-serif",
    "serif",
    "monospace",
    "system-ui",
    "ui-monospace",
    "ui-sans-serif",
    "ui-serif",
    "helvetica",
    "helvetica neue",
    "arial",
    "georgia",
    "times new roman",
    "courier new",
    "verdana",
    "tahoma",
    "trebuchet ms",
    "-apple-system",
    "blinkmacsystemfont",
  ]);
  const seenIds = new Map();
  const rasterEffects = [];
  const hasOwnText = (el) =>
    Array.prototype.some.call(
      el.childNodes,
      (n) => n.nodeType === 3 && n.nodeValue.trim().length > 0,
    );
  slides.forEach((slide, index) => {
    const label = slide.getAttribute("data-screen-label") || "";
    const slideId = slide.getAttribute("data-superchat-id") || "";
    const add = (el, severity, code, message) =>
      issues.push({
        slide: index + 1,
        label,
        id: (el && el.getAttribute && el.getAttribute("data-superchat-id")) || slideId,
        severity,
        code,
        message,
      });
    if (!label) add(slide, "error", "slide-label", "The slide has no data-screen-label.");
    if (!slideId) add(slide, "error", "slide-id", "The slide has no data-superchat-id.");
    const box = slide.getBoundingClientRect();
    for (const el of slide.querySelectorAll("*")) {
      if (el.closest(".foot")) continue;
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") continue;
      const tag = el.tagName.toLowerCase();
      if (tag === "script" || tag === "style" || tag === "br") continue;
      const id = el.getAttribute("data-superchat-id");
      if (id) {
        if (seenIds.has(id))
          add(el, "error", "duplicate-id", `data-superchat-id "${id}" is used more than once.`);
        seenIds.set(id, true);
      }
      const isText = hasOwnText(el);
      const isMedia = tag === "img" || tag === "svg" || tag === "canvas" || tag === "video";
      const rect = el.getBoundingClientRect();
      if ((isText || isMedia) && rect.width > 0 && rect.height > 0) {
        if (
          rect.left < box.left - 1 ||
          rect.right > box.right + 1 ||
          rect.top < box.top - 1 ||
          rect.bottom > box.bottom + 1
        ) {
          add(el, "error", "outside-slide", "This content runs past the edge of the slide.");
        } else if (rect.bottom - box.top > railTop + 1) {
          add(
            el,
            "error",
            "footer-rail",
            "This content runs into the footer band; move it up or split the slide.",
          );
        }
        if (
          el.scrollWidth > el.clientWidth + 1 &&
          style.display !== "inline" &&
          style.overflowX !== "visible"
        ) {
          add(el, "error", "clipped-text", "This text is wider than its box and gets clipped.");
        }
        if (
          el.scrollHeight > el.clientHeight + 1 &&
          style.display !== "inline" &&
          style.overflowY !== "visible"
        ) {
          add(el, "error", "clipped-text", "This text is taller than its box and gets clipped.");
        }
      }
      if ((isText || isMedia) && style.display !== "inline" && !id) {
        add(el, "error", "missing-id", `A <${tag}> with content has no data-superchat-id.`);
      }
      if (isText) {
        const size = parseFloat(style.fontSize);
        if (size < 18)
          add(
            el,
            "warning",
            "small-text",
            `Text is ${Math.round(size)}px; keep body text at 24px or more.`,
          );
        const first = style.fontFamily
          .split(",")[0]
          .trim()
          .replace(/^["']|["']$/g, "")
          .toLowerCase();
        if (first && !embedded.has(first) && !systemOk.has(first)) {
          add(
            el,
            "warning",
            "font-not-embedded",
            `Font "${first}" is not embedded in this deck; PowerPoint will substitute it.`,
          );
        }
        if (/^h[1-3]$/.test(tag) || el.classList.contains("title")) {
          const words = (el.textContent || "").trim().split(/\s+/).filter(Boolean).length;
          if (words > 12)
            add(el, "warning", "long-headline", `Headline is ${words} words; aim for 8 or fewer.`);
        }
      }
      const raster = [];
      if (style.filter && style.filter !== "none") raster.push("filter");
      if (style.backdropFilter && style.backdropFilter !== "none") raster.push("backdrop-filter");
      if (style.mixBlendMode && style.mixBlendMode !== "normal") raster.push("mix-blend-mode");
      if (style.maskImage && style.maskImage !== "none") raster.push("mask");
      if (/conic-gradient|repeating-/.test(style.backgroundImage))
        raster.push("conic or repeating gradient");
      if (raster.length)
        rasterEffects.push({ el, slide: index + 1, label, what: raster.join(", ") });
    }
  });
  // Superchat addition: a chart must be able to become a native PowerPoint chart.
  for (const chartIssue of lintCharts(slideSelector)) {
    issues.push({
      slide: chartIssue.slide,
      label: slides[chartIssue.slide - 1]?.getAttribute("data-screen-label") || "",
      id: chartIssue.id,
      severity: "error",
      code: "chart-not-native",
      message: chartIssue.message,
    });
  }
  for (const r of rasterEffects) {
    issues.push({
      slide: r.slide,
      label: r.label,
      id: r.el.getAttribute("data-superchat-id") || "",
      severity: "note",
      code: "picture-layer",
      message: `${r.what} becomes a picture layer behind the text in PowerPoint (the text stays editable).`,
    });
  }
  return issues;
}

// ---------------------------------------------------------------------------------------------
// Native charts (Superchat addition, not in Open Design).
//
// A deck chart is Chart.js on a <canvas> (the ported chart-*.html templates). At export each
// Chart.js instance is read as it was configured (type, labels, datasets, colors, axes, legend,
// number formats) and turned into the PptxGenJS chart spec that the vendored dom-to-pptx hook
// (`<canvas data-pptx-chart>`, see vendor/dom-to-pptx/README.md) hands to `slide.addChart`. The
// result is a real PowerPoint chart with an embedded workbook, never a picture of the canvas.
// A chart with no PowerPoint equivalent throws a sentence the Muse can act on; `deck_check` shows it.
// ---------------------------------------------------------------------------------------------

const NATIVE_CHART_HINT =
  "Use a bar, line, area, pie, doughnut, radar, scatter or bubble chart (or a bar with a line), or a table.";
const COMBO_KINDS = ["bar", "line", "area"];

function chartPlainFontFamily(family) {
  const first = String(family || "")
    .split(",")[0]
    .trim()
    .replace(/^["']|["']$/g, "");
  return first || undefined;
}

// CSS color -> { hex: "RRGGBB", alpha: 0..1 }. Hex and rgb() are parsed directly; names and hsl()
// go through a canvas, which normalises any CSS color the browser knows.
export function parseChartColor(value, what) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(
      `${what} must be a plain CSS color; a gradient or pattern has no native PowerPoint chart fill.`,
    );
  }
  const v = value.trim();
  const hex = /^#([0-9a-f]{3,8})$/i.exec(v);
  if (hex) {
    let h = hex[1];
    if (h.length === 3 || h.length === 4) {
      h = Array.from(h)
        .map((c) => c + c)
        .join("");
    }
    if (h.length === 6) return { hex: h.toUpperCase(), alpha: 1 };
    if (h.length === 8) {
      return { hex: h.slice(0, 6).toUpperCase(), alpha: parseInt(h.slice(6), 16) / 255 };
    }
  }
  const rgb =
    /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/i.exec(v);
  if (rgb) {
    const channel = (n) =>
      Math.max(0, Math.min(255, Math.round(Number(n))))
        .toString(16)
        .padStart(2, "0");
    const alpha = rgb[4] === undefined ? 1 : rgb[5] ? Number(rgb[4]) / 100 : Number(rgb[4]);
    return { hex: (channel(rgb[1]) + channel(rgb[2]) + channel(rgb[3])).toUpperCase(), alpha };
  }
  const context = document.createElement("canvas").getContext("2d");
  const read = (sentinel) => {
    context.fillStyle = sentinel;
    context.fillStyle = v;
    return context.fillStyle;
  };
  const a = read("#000000");
  if (a !== read("#ffffff")) throw new Error(`${what} "${v}" is not a CSS color.`);
  return parseChartColor(a, what);
}

// Blend a translucent color onto the surface behind the chart: PowerPoint grid lines and axis
// text take a color but no alpha.
function blendOnto(color, backdropHex) {
  if (color.alpha >= 0.999) return color.hex;
  const mix = (i) => {
    const front = parseInt(color.hex.slice(i, i + 2), 16);
    const back = parseInt(backdropHex.slice(i, i + 2), 16);
    return Math.round(front * color.alpha + back * (1 - color.alpha))
      .toString(16)
      .padStart(2, "0");
  };
  return (mix(0) + mix(2) + mix(4)).toUpperCase();
}

function surfaceBehind(canvas) {
  for (let el = canvas; el; el = el.parentElement) {
    const bg = getComputedStyle(el).backgroundColor;
    if (!bg || bg === "transparent") continue;
    const c = parseChartColor(bg, "background");
    if (c.alpha > 0.5) return c.hex;
  }
  return "FFFFFF";
}

// Intl.NumberFormat options (what Chart.js' `ticks.format` takes) -> an Excel number format code.
function excelNumberFormat(format) {
  if (!format || typeof format !== "object") return undefined;
  if (format.notation === "compact") {
    throw new Error(
      "A compact number format (notation: 'compact') has no PowerPoint number format.",
    );
  }
  const digits =
    format.minimumFractionDigits ??
    format.maximumFractionDigits ??
    (format.style === "currency" ? 2 : 0);
  const zeros = digits > 0 ? `.${"0".repeat(digits)}` : "";
  const base = (format.useGrouping === false ? "0" : "#,##0") + zeros;
  if (format.style === "percent") return `${base}%`;
  if (format.style === "currency") {
    const symbols = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", CHF: "CHF ", CAD: "$", AUD: "$" };
    const symbol = symbols[format.currency] ?? `${format.currency} `;
    return `"${symbol}"${base}`;
  }
  return base;
}

function numberOrNull(value, what) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
    return Number(value);
  }
  throw new Error(`${what} holds a value that is not a number.`);
}

// What kind of PowerPoint series a dataset is. A filled line is an area chart.
function datasetKind(chart, index, topType) {
  const dataset = chart.data.datasets[index];
  const type = dataset.type || topType;
  if (type === "line" || type === "radar") {
    const meta = chart.getDatasetMeta ? chart.getDatasetMeta(index) : null;
    const fill = meta?.dataset?.options?.fill ?? dataset.fill;
    const filled = fill !== undefined && fill !== null && fill !== false && fill !== "";
    return type === "line" ? (filled ? "area" : "line") : "radar";
  }
  return type;
}

function elementColor(chart, index, point, field, what) {
  const dataset = chart.data.datasets[index];
  const meta = chart.getDatasetMeta ? chart.getDatasetMeta(index) : null;
  let raw;
  if (point === null) {
    raw = meta?.dataset?.options?.[field] ?? dataset[field];
  } else {
    raw = meta?.data?.[point]?.options?.[field];
    if (raw === undefined)
      raw = Array.isArray(dataset[field]) ? dataset[field][point] : dataset[field];
  }
  if (Array.isArray(raw)) raw = raw[point ?? 0];
  return parseChartColor(raw, what);
}

function scaleSettings(scale, backdrop, face) {
  // Shared axis facts: label color, size and face, title, grid. `prefix` is cat or val.
  const out = {};
  if (!scale) return out;
  const ticks = scale.ticks || {};
  if (ticks.color)
    out.labelColor = blendOnto(parseChartColor(ticks.color, "an axis label color"), backdrop);
  if (ticks.font?.size) out.labelFontSize = ticks.font.size;
  out.labelFontFace = face;
  out.hidden = scale.display === false;
  const grid = scale.grid || {};
  if (grid.display === false) {
    out.gridLine = { style: "none" };
  } else if (grid.color && typeof grid.color === "string") {
    out.gridLine = {
      color: blendOnto(parseChartColor(grid.color, "a grid color"), backdrop),
      size: grid.lineWidth && typeof grid.lineWidth === "number" ? grid.lineWidth : 1,
      style: "solid",
    };
  }
  const title = scale.title || {};
  if (title.display && title.text) {
    out.title = Array.isArray(title.text) ? title.text.join(" ") : String(title.text);
    if (title.color)
      out.titleColor = blendOnto(parseChartColor(title.color, "an axis title color"), backdrop);
    if (title.font?.size) out.titleFontSize = title.font.size;
  }
  return out;
}

function applyAxis(target, prefix, settings, extra = {}) {
  // prefix: "cat" | "val"
  const set = (key, value) => {
    if (value !== undefined) target[`${prefix}${key}`] = value;
  };
  if (settings.hidden) set("AxisHidden", true);
  set("AxisLabelColor", settings.labelColor);
  set("AxisLabelFontSize", settings.labelFontSize);
  set("AxisLabelFontFace", settings.labelFontFace);
  set("GridLine", settings.gridLine);
  if (settings.title) {
    target[`show${prefix === "cat" ? "Cat" : "Val"}AxisTitle`] = true;
    set("AxisTitle", settings.title);
    set("AxisTitleColor", settings.titleColor);
    set("AxisTitleFontSize", settings.titleFontSize);
    set("AxisTitleFontFace", settings.labelFontFace);
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined) target[key] = value;
  }
}

function valueScaleBounds(scale, values) {
  const out = {};
  if (!scale) return out;
  const finite = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  const dataMin = finite.length ? Math.min(...finite) : 0;
  const dataMax = finite.length ? Math.max(...finite) : 0;
  let min = scale.min;
  let max = scale.max;
  if (min === undefined && typeof scale.suggestedMin === "number")
    min = Math.min(scale.suggestedMin, dataMin);
  if (max === undefined && typeof scale.suggestedMax === "number")
    max = Math.max(scale.suggestedMax, dataMax);
  if (min === undefined && scale.beginAtZero === true && dataMin >= 0) min = 0;
  if (typeof min === "number") out.min = min;
  if (typeof max === "number") out.max = max;
  if (typeof scale.ticks?.stepSize === "number") out.majorUnit = scale.ticks.stepSize;
  const format = excelNumberFormat(scale.ticks?.format);
  if (format) out.format = format;
  return out;
}

function symbolFor(style, radius) {
  if (radius === 0) return "none";
  const map = {
    circle: "circle",
    rect: "square",
    rectRounded: "square",
    triangle: "triangle",
    rectRot: "diamond",
    dash: "dash",
    line: "dash",
  };
  if (style === undefined || style === false) return "circle";
  if (!(style in map)) {
    throw new Error(
      `The point style "${style}" has no PowerPoint marker. Use circle, rect, triangle or rectRot.`,
    );
  }
  return map[style];
}

// Everything the chart-level options carry: title, legend, font. Axes are added per chart family.
function chartFrameOptions(chart, backdrop, face) {
  const out = {};
  const plugins = chart.options?.plugins || {};
  const title = plugins.title || {};
  if (title.display && title.text) {
    out.showTitle = true;
    out.title = Array.isArray(title.text) ? title.text.join(" ") : String(title.text);
    if (title.color)
      out.titleColor = blendOnto(parseChartColor(title.color, "the chart title color"), backdrop);
    if (title.font?.size) out.titleFontSize = title.font.size;
    out.titleFontFace = face;
  } else {
    out.showTitle = false;
  }
  const legend = plugins.legend || {};
  if (legend.display === false) {
    out.showLegend = false;
  } else {
    out.showLegend = true;
    const positions = { top: "t", bottom: "b", left: "l", right: "r" };
    out.legendPos = positions[legend.position || "top"] || "t";
    const labels = legend.labels || {};
    if (labels.color)
      out.legendColor = blendOnto(parseChartColor(labels.color, "the legend color"), backdrop);
    if (labels.font?.size) out.legendFontSize = labels.font.size;
    out.legendFontFace = face;
  }
  return out;
}

// Chart.js instance -> { types: [{ type, data, options }], options }, the data-pptx-chart payload.
export function chartJsToPptx(chart, canvas) {
  const topType = chart.config?.type ?? chart.config?._config?.type;
  const backdrop = surfaceBehind(canvas);
  const defaultFace = chartPlainFontFamily(window.Chart?.defaults?.font?.family);
  const resolvedFace = chartPlainFontFamily(chart.options?.font?.family);
  const face =
    resolvedFace && resolvedFace !== defaultFace
      ? resolvedFace
      : chartPlainFontFamily(getComputedStyle(canvas).fontFamily);
  const datasetIndexes = chart.data.datasets
    .map((_dataset, index) => index)
    .filter((index) => !chart.data.datasets[index].hidden);
  if (datasetIndexes.length === 0) throw new Error("The chart has no visible dataset.");
  const kinds = datasetIndexes.map((index) => datasetKind(chart, index, topType));
  const kindSet = Array.from(new Set(kinds));
  const supported = ["bar", "line", "area", "pie", "doughnut", "radar", "scatter", "bubble"];
  for (const kind of kindSet) {
    if (!supported.includes(kind)) {
      throw new Error(
        `A ${kind} chart has no native PowerPoint equivalent (PowerPoint cannot draw it as a chart). ${NATIVE_CHART_HINT}`,
      );
    }
  }
  if (kindSet.length > 1 && !kindSet.every((kind) => COMBO_KINDS.includes(kind))) {
    throw new Error(
      `PowerPoint combines only bar, line and area datasets in one chart; ${kindSet.join(" with ")} cannot share a chart. ${NATIVE_CHART_HINT}`,
    );
  }
  const frame = chartFrameOptions(chart, backdrop, face);
  const labels = (chart.data.labels || []).map((label) =>
    Array.isArray(label) ? label.join(" ") : String(label),
  );
  const kind = kindSet[0];
  if (kindSet.length === 1 && (kind === "pie" || kind === "doughnut")) {
    return pieSpec(chart, datasetIndexes, kind, labels, frame);
  }
  if (kindSet.length === 1 && (kind === "scatter" || kind === "bubble")) {
    return xySpec(chart, datasetIndexes, kind, frame, backdrop, face);
  }
  if (kindSet.length === 1 && kind === "radar") {
    return radarSpec(chart, datasetIndexes, labels, frame, backdrop, face);
  }
  return categorySpec(chart, datasetIndexes, kinds, labels, frame, backdrop, face);
}

function pieSpec(chart, indexes, kind, labels, frame) {
  if (indexes.length > 1) {
    throw new Error(
      `A ${kind} chart has one series in PowerPoint; this one has ${indexes.length} datasets (rings). Use one dataset, or a bar chart.`,
    );
  }
  const index = indexes[0];
  const dataset = chart.data.datasets[index];
  const values = dataset.data.map((value) => numberOrNull(value, `Dataset "${dataset.label}"`));
  const circumference = chart.options?.circumference;
  if (circumference !== undefined && circumference !== 360) {
    throw new Error(
      `A ${kind} chart with circumference ${circumference} (a gauge) has no native PowerPoint equivalent. ${NATIVE_CHART_HINT}`,
    );
  }
  const options = {
    ...frame,
    chartColors: values.map((_v, point) => {
      const color = elementColor(
        chart,
        index,
        point,
        "backgroundColor",
        `Slice ${point + 1} of "${dataset.label}"`,
      );
      return color.hex;
    }),
    showPercent: false,
    showValue: false,
    showLabel: false,
  };
  const rotation = chart.options?.rotation;
  if (typeof rotation === "number")
    options.firstSliceAng = ((Math.round(rotation) % 360) + 360) % 360;
  const borderWidth = elementBorderWidth(chart, index);
  if (borderWidth > 0) {
    options.dataBorder = {
      pt: borderWidth,
      color: elementColor(chart, index, 0, "borderColor", "the slice border color").hex,
    };
  }
  if (kind === "doughnut") {
    const cutout = chart.options?.cutout;
    let hole = 50;
    if (typeof cutout === "string" && cutout.endsWith("%")) hole = parseFloat(cutout);
    else if (typeof cutout === "number" && chart.outerRadius)
      hole = (cutout / chart.outerRadius) * 100;
    options.holeSize = Math.max(10, Math.min(90, Math.round(hole)));
  }
  const seriesLabels = labels.length ? labels : values.map((_v, i) => `Item ${i + 1}`);
  return {
    types: [
      {
        type: kind,
        data: [{ name: String(dataset.label ?? "Series 1"), labels: seriesLabels, values }],
        options: {},
      },
    ],
    options,
  };
}

function elementBorderWidth(chart, index) {
  const meta = chart.getDatasetMeta ? chart.getDatasetMeta(index) : null;
  const fromElement = meta?.data?.[0]?.options?.borderWidth;
  const raw = fromElement ?? chart.data.datasets[index].borderWidth;
  return typeof raw === "number" ? raw : 0;
}

function radarSpec(chart, indexes, labels, frame, backdrop, face) {
  const scale = chart.options?.scales?.r || {};
  const series = indexes.map((index) => seriesFor(chart, index, labels));
  const colors = indexes.map((index) =>
    elementColor(
      chart,
      index,
      null,
      "borderColor",
      `The border of "${chart.data.datasets[index].label}"`,
    ),
  );
  const filled = indexes.some(
    (index) => datasetKind(chart, index, "radar") === "radar" && radarFilled(chart, index),
  );
  const options = { ...frame, chartColors: colors.map((c) => c.hex) };
  if (filled) {
    const fills = indexes.map((index) =>
      elementColor(chart, index, null, "backgroundColor", "a radar fill"),
    );
    options.radarStyle = "filled";
    options.chartColors = fills.map((c) => c.hex);
    options.chartColorsOpacity = Math.round(fills[0].alpha * 100);
  } else {
    const radius = chart.data.datasets[indexes[0]].pointRadius;
    options.radarStyle = radius === 0 ? "standard" : "marker";
  }
  const bounds = valueScaleBounds(
    scale,
    series.flatMap((s) => s.values),
  );
  const grid = scaleSettings(scale, backdrop, face);
  applyAxis(
    options,
    "val",
    {
      gridLine: grid.gridLine,
      labelFontFace: face,
      labelColor: grid.labelColor,
      labelFontSize: grid.labelFontSize,
      hidden: scale.ticks?.display === false,
    },
    {
      valAxisMinVal: bounds.min,
      valAxisMaxVal: bounds.max,
      valAxisMajorUnit: bounds.majorUnit,
      valAxisLabelFormatCode: bounds.format,
    },
  );
  const pointLabels = scale.pointLabels || {};
  if (pointLabels.color)
    options.catAxisLabelColor = blendOnto(
      parseChartColor(pointLabels.color, "the radar label color"),
      backdrop,
    );
  if (pointLabels.font?.size) options.catAxisLabelFontSize = pointLabels.font.size;
  options.catAxisLabelFontFace = face;
  const borderWidth = elementBorderWidth(chart, indexes[0]);
  if (borderWidth > 0) options.lineSize = borderWidth;
  return { types: [{ type: "radar", data: series, options: {} }], options };
}

function radarFilled(chart, index) {
  const meta = chart.getDatasetMeta ? chart.getDatasetMeta(index) : null;
  const fill = meta?.dataset?.options?.fill ?? chart.data.datasets[index].fill;
  return fill !== undefined && fill !== null && fill !== false && fill !== "";
}

function seriesFor(chart, index, labels) {
  const dataset = chart.data.datasets[index];
  const name = String(dataset.label ?? `Series ${index + 1}`);
  const values = dataset.data.map((value) => {
    if (value && typeof value === "object" && "y" in value)
      return numberOrNull(value.y, `Dataset "${name}"`);
    return numberOrNull(value, `Dataset "${name}"`);
  });
  return { name, labels: labels.length ? labels : values.map((_v, i) => String(i + 1)), values };
}

function xySpec(chart, indexes, kind, frame, backdrop, face) {
  const scales = chart.options?.scales || {};
  const points = indexes.map((index) => {
    const dataset = chart.data.datasets[index];
    const name = String(dataset.label ?? `Series ${index + 1}`);
    return {
      index,
      name,
      points: dataset.data.map((p) => {
        if (!p || typeof p !== "object" || !("x" in p) || !("y" in p)) {
          throw new Error(
            `Dataset "${name}" needs {x, y${kind === "bubble" ? ", r" : ""}} points.`,
          );
        }
        return {
          x: numberOrNull(p.x, name),
          y: numberOrNull(p.y, name),
          r: kind === "bubble" ? numberOrNull(p.r, name) : undefined,
        };
      }),
    };
  });
  // PowerPoint's scatter and bubble charts share one X column: series with the same x values use
  // it directly; series with different x values are laid on the union of x values with gaps.
  const sameX = points.every(
    (s) =>
      s.points.length === points[0].points.length &&
      s.points.every((p, i) => p.x === points[0].points[i].x),
  );
  let xs;
  const columns = points.map((s) => ({ name: s.name, values: [], sizes: [] }));
  if (sameX) {
    xs = points[0].points.map((p) => p.x);
    points.forEach((s, i) => {
      columns[i].values = s.points.map((p) => p.y);
      columns[i].sizes = s.points.map((p) => p.r ?? 1);
    });
  } else {
    xs = Array.from(new Set(points.flatMap((s) => s.points.map((p) => p.x)))).sort((a, b) => a - b);
    points.forEach((s, i) => {
      const byX = new Map();
      for (const p of s.points) {
        if (byX.has(p.x)) {
          throw new Error(
            `Dataset "${s.name}" repeats an x value. PowerPoint's ${kind} chart has one x column: give each series distinct x values, or the same x values in every series.`,
          );
        }
        byX.set(p.x, p);
      }
      columns[i].values = xs.map((x) => byX.get(x)?.y ?? null);
      columns[i].sizes = xs.map((x) => byX.get(x)?.r ?? 1);
    });
  }
  const data = [
    { name: "X-Axis", values: xs },
    ...columns.map((c) => (kind === "bubble" ? c : { name: c.name, values: c.values })),
  ];
  const colors = points.map(
    (s) => elementColor(chart, s.index, null, "backgroundColor", `The color of "${s.name}"`).hex,
  );
  const options = { ...frame, chartColors: colors };
  if (kind === "bubble") {
    options.chartColorsOpacity = Math.round(
      elementColor(chart, points[0].index, null, "backgroundColor", "the bubble color").alpha * 100,
    );
  } else {
    const showLine = chart.data.datasets[points[0].index].showLine === true;
    options.lineSize = showLine ? elementBorderWidth(chart, points[0].index) || 2 : 0;
    const radius =
      chart.data.datasets[points[0].index].pointRadius ??
      chart.options?.elements?.point?.radius ??
      3;
    options.lineDataSymbol = symbolFor(chart.data.datasets[points[0].index].pointStyle, radius);
    options.lineDataSymbolSize = Math.max(2, radius * 2);
  }
  const xBounds = valueScaleBounds(scales.x, xs);
  const yBounds = valueScaleBounds(
    scales.y,
    columns.flatMap((c) => c.values),
  );
  applyAxis(options, "cat", scaleSettings(scales.x, backdrop, face), {
    catAxisMinVal: xBounds.min,
    catAxisMaxVal: xBounds.max,
    catAxisMajorUnit: xBounds.majorUnit,
    catAxisLabelFormatCode: xBounds.format,
  });
  applyAxis(options, "val", scaleSettings(scales.y, backdrop, face), {
    valAxisMinVal: yBounds.min,
    valAxisMaxVal: yBounds.max,
    valAxisMajorUnit: yBounds.majorUnit,
    valAxisLabelFormatCode: yBounds.format,
  });
  return { types: [{ type: kind, data, options: {} }], options };
}

// bar / line / area, alone or combined, on one or two value axes.
function categorySpec(chart, indexes, kinds, labels, frame, backdrop, face) {
  const horizontal = chart.options?.indexAxis === "y";
  const scales = chart.options?.scales || {};
  const categoryScale = scales[horizontal ? "y" : "x"] || {};
  const valueScaleId = (index) => {
    const dataset = chart.data.datasets[index];
    return (horizontal ? dataset.xAxisID : dataset.yAxisID) || (horizontal ? "x" : "y");
  };
  const axisIds = Array.from(new Set(indexes.map(valueScaleId)));
  if (axisIds.length > 2) {
    throw new Error(
      "PowerPoint has a primary and a secondary value axis; this chart uses three or more.",
    );
  }
  const groups = [];
  indexes.forEach((index, position) => {
    const kind = kinds[position];
    const axis = axisIds.indexOf(valueScaleId(index));
    let group = groups.find((g) => g.kind === kind && g.axis === axis);
    if (!group) {
      group = { kind, axis, indexes: [] };
      groups.push(group);
    }
    group.indexes.push(index);
  });
  const stackedOn = (id) => Boolean(scales[id]?.stacked);
  const categoryStacked = Boolean(categoryScale.stacked);
  const types = groups.map((group) => {
    const options = {};
    const series = group.indexes.map((index) => seriesFor(chart, index, labels));
    const stacked = categoryStacked && stackedOn(axisIds[group.axis]);
    if (group.kind === "bar") {
      options.barDir = horizontal ? "bar" : "col";
      options.barGrouping = stacked ? "stacked" : "clustered";
      const first = chart.data.datasets[group.indexes[0]];
      const barPct = first.barPercentage ?? chart.options?.datasets?.bar?.barPercentage ?? 0.9;
      const catPct =
        first.categoryPercentage ?? chart.options?.datasets?.bar?.categoryPercentage ?? 0.8;
      const n = stacked ? 1 : group.indexes.length;
      options.barGapWidthPct = Math.max(
        0,
        Math.min(500, Math.round(n * (1 / (barPct * catPct) - 1) * 100)),
      );
      const perPoint =
        group.indexes.length === 1 &&
        Array.isArray(chart.data.datasets[group.indexes[0]].backgroundColor);
      if (perPoint) {
        options.chartColors = series[0].values.map(
          (_v, point) =>
            elementColor(chart, group.indexes[0], point, "backgroundColor", `Bar ${point + 1}`).hex,
        );
      } else {
        options.chartColors = group.indexes.map((index) => {
          if (Array.isArray(chart.data.datasets[index].backgroundColor)) {
            throw new Error(
              `Dataset "${chart.data.datasets[index].label}" colors each bar separately while sharing the chart with other datasets; PowerPoint colors a bar series with one color. Give each dataset one color.`,
            );
          }
          return elementColor(
            chart,
            index,
            null,
            "backgroundColor",
            `The color of "${chart.data.datasets[index].label}"`,
          ).hex;
        });
      }
    } else if (group.kind === "line") {
      if (stacked) {
        throw new Error(
          "A stacked line chart has no native PowerPoint equivalent here. Use a stacked area or stacked bar chart.",
        );
      }
      options.chartColors = group.indexes.map(
        (index) =>
          elementColor(
            chart,
            index,
            null,
            "borderColor",
            `The line color of "${chart.data.datasets[index].label}"`,
          ).hex,
      );
      const datasets = group.indexes.map((index) => chart.data.datasets[index]);
      const width = Math.max(
        ...group.indexes.map((index) => elementBorderWidth(chart, index) || 3),
      );
      options.lineSize = width;
      const tension = datasets.some(
        (d) => (d.tension ?? chart.options?.elements?.line?.tension ?? 0) > 0,
      );
      options.lineSmooth = tension;
      const radius = datasets[0].pointRadius ?? chart.options?.elements?.point?.radius ?? 3;
      options.lineDataSymbol = symbolFor(datasets[0].pointStyle, radius);
      options.lineDataSymbolSize = Math.max(2, radius * 2);
      options.lineDash = datasets.map((d) =>
        Array.isArray(d.borderDash) && d.borderDash.length ? "dash" : "solid",
      );
    } else {
      options.barGrouping = stacked ? "stacked" : "standard";
      const fills = group.indexes.map((index) =>
        elementColor(
          chart,
          index,
          null,
          "backgroundColor",
          `The fill of "${chart.data.datasets[index].label}"`,
        ),
      );
      options.chartColors = fills.map((c) => c.hex);
      options.chartColorsOpacity = Math.round(fills[0].alpha * 100);
    }
    if (group.axis === 1) {
      options.secondaryValAxis = true;
      options.secondaryCatAxis = true;
    }
    return { type: group.kind, data: series, options };
  });

  const allValues = types.flatMap((t) => t.data.flatMap((s) => s.values));
  const category = scaleSettings(categoryScale, backdrop, face);
  const valueSettings = axisIds.map((id) => scaleSettings(scales[id], backdrop, face));
  const boundsFor = (id, group) => {
    const values = types
      .filter((_t, i) => groups[i].axis === group)
      .flatMap((t) => t.data.flatMap((s) => s.values));
    return valueScaleBounds(scales[id], values);
  };
  const options = { ...frame };
  const axisExtras = (bounds) => ({
    valAxisMinVal: bounds.min,
    valAxisMaxVal: bounds.max,
    valAxisMajorUnit: bounds.majorUnit,
    valAxisLabelFormatCode: bounds.format,
  });
  if (axisIds.length === 1) {
    applyAxis(options, "cat", category);
    applyAxis(options, "val", valueSettings[0], axisExtras(boundsFor(axisIds[0], 0)));
  } else {
    const catPrimary = {};
    applyAxis(catPrimary, "cat", category);
    const catSecondary = { catAxisHidden: true };
    const valPrimary = {};
    applyAxis(valPrimary, "val", valueSettings[0], axisExtras(boundsFor(axisIds[0], 0)));
    const valSecondary = {};
    applyAxis(valSecondary, "val", valueSettings[1], axisExtras(boundsFor(axisIds[1], 1)));
    options.catAxes = [catPrimary, catSecondary];
    options.valAxes = [valPrimary, valSecondary];
  }
  const barKinds = kinds.includes("bar");
  if (horizontal && barKinds) {
    // Chart.js lists categories top to bottom; PowerPoint draws the first one at the bottom.
    options.catAxisOrientation = "maxMin";
    options.valAxisLabelPos = "high";
  }
  if (allValues.some((v) => v === undefined)) throw new Error("The chart data is incomplete.");
  return { types, options };
}

// The Chart.js instance behind a canvas, or a sentence saying why there is none.
export function chartForCanvas(canvas) {
  const registry = window.Chart;
  const chart =
    registry && typeof registry.getChart === "function" ? registry.getChart(canvas) : null;
  if (!chart) {
    throw new Error(
      "A <canvas> that is not a Chart.js chart cannot become a native PowerPoint chart. Draw it as a Chart.js chart, a table, or HTML shapes.",
    );
  }
  return chart;
}

export function nativeChartSpec(canvas) {
  return chartJsToPptx(chartForCanvas(canvas), canvas);
}

function chartCanvases(slideSelector) {
  const slides = Array.prototype.slice
    .call(document.querySelectorAll(slideSelector))
    .filter((el) => !el.closest(".mini-slide, .overview, .notes-overlay, .thumb"));
  const found = [];
  slides.forEach((slide, index) => {
    slide.querySelectorAll("canvas").forEach((canvas) => found.push({ canvas, slide: index + 1 }));
  });
  return found;
}

// Export-time: put each chart's PptxGenJS spec on its canvas for dom-to-pptx. Throws a sentence
// naming the slide when a chart cannot be native.
export function markNativeCharts(slideSelector) {
  let count = 0;
  for (const { canvas, slide } of chartCanvases(slideSelector)) {
    let spec;
    try {
      spec = nativeChartSpec(canvas);
    } catch (error) {
      throw new Error(`Slide ${slide}: ${error instanceof Error ? error.message : String(error)}`);
    }
    canvas.setAttribute("data-pptx-chart", JSON.stringify(spec));
    count += 1;
  }
  return count;
}

// Re-layout every chart at the stage's final size (the PDF prints the canvases as painted).
export function resizeCharts() {
  const registry = window.Chart;
  if (!registry || typeof registry.getChart !== "function") return 0;
  let count = 0;
  document.querySelectorAll("canvas").forEach((canvas) => {
    const chart = registry.getChart(canvas);
    if (chart) {
      chart.resize();
      chart.update("none");
      count += 1;
    }
  });
  return count;
}

// Lint: every canvas of the slide must be a Chart.js chart PowerPoint can draw natively.
export function lintCharts(slideSelector) {
  const issues = [];
  for (const { canvas, slide } of chartCanvases(slideSelector)) {
    try {
      nativeChartSpec(canvas);
    } catch (error) {
      issues.push({
        slide,
        id:
          canvas.getAttribute("data-superchat-id") ||
          canvas.closest("[data-superchat-id]")?.getAttribute("data-superchat-id") ||
          "",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return issues;
}
