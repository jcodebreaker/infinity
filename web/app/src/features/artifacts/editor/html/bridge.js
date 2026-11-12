// biome-ignore-all lint: a plain ES5-style script that runs inside the sandboxed deck frame
// Portions modified from nexu-io/open-design apps/web/src/edit-mode/bridge.ts@802708f, Apache-2.0;
// changes: rewritten for Superchat decks. The nonce is baked in and checked on every message together
// with event.source, messages are the superchat:edit-* protocol (see artifact-edit-protocol.ts), targets are
// `data-superchat-id` elements on the active slide only, selection chrome is a separate overlay layer
// (no attributes or classes are written to the deck), multi-select, keyboard nudge, and no
// drag / guides / brand-kit / link-preview handling.
//
// Page mode (any HTML page or mini app, __SUPERCHAT_MODE__ = "page"): every element of the body
// is a target; one without a data-superchat-id gets a "~" path id (child indexes from <body>),
// which is good for selecting and asking superchat but never for a hand edit (it is no stable
// address, so the host and the engine refuse it).
//
// Runs inside the sandboxed frame (srcdoc, no same-origin). __SUPERCHAT_NONCE__ and
// __SUPERCHAT_MODE__ are replaced by the host before injection. It must be the first script of the document, so its capture
// listeners run before the deck's own click and key handlers and can stop them.
(() => {
  var NONCE = "__SUPERCHAT_NONCE__";
  var PAGE = "__SUPERCHAT_MODE__" === "page";
  var V = 1;
  // Inline runs and icon parts: a click on one means the element around it.
  var INLINE =
    /^(span|strong|em|b|i|u|s|small|mark|code|sup|sub|abbr|svg|path|g|use|circle|rect|line|polyline|polygon|ellipse)$/i;
  var NOT_TARGET = /^(script|style|head|meta|link|title|br|template|noscript)$/i;
  var layer = null;
  var selected = [];
  var hovered = null;
  var editing = null; // { el, before }
  var lastRects = "";
  var raf = 0;

  function send(msg) {
    msg.v = V;
    msg.nonce = NONCE;
    try {
      window.parent.postMessage(msg, "*");
    } catch (e) {}
  }
  function esc(id) {
    return window.CSS && CSS.escape ? CSS.escape(id) : String(id).replace(/["\\]/g, "\\$&");
  }
  function slideOf(el) {
    return el.closest ? el.closest(".slide") : null;
  }
  function slides() {
    return Array.prototype.slice.call(document.querySelectorAll("section.slide, .slide"));
  }
  function inLayer(el) {
    return !!layer && (el === layer || layer.contains(el));
  }
  function stableId(el) {
    return el.getAttribute("data-superchat-id");
  }
  // A page element's place: its child index under each ancestor up to <body>.
  function pathOf(el) {
    var parts = [];
    var cur = el;
    while (cur && cur !== document.body) {
      var parent = cur.parentElement;
      if (!parent) return "";
      parts.unshift(Array.prototype.indexOf.call(parent.children, cur));
      cur = parent;
    }
    return "~" + parts.join(".");
  }
  function idOf(el) {
    return stableId(el) || pathOf(el);
  }
  function findById(id) {
    if (PAGE && id.charAt(0) === "~") {
      var cur = document.body;
      var parts = id.slice(1).split(".");
      for (var i = 0; i < parts.length && cur; i++) cur = cur.children[Number(parts[i])] || null;
      return cur && cur !== document.body ? cur : null;
    }
    return document.querySelector('[data-superchat-id="' + esc(id) + '"]');
  }
  // Where a page element sits, for superchat when it has no id: `main > form#add > button`.
  function whereOf(el) {
    var out = [];
    var cur = el;
    for (var i = 0; i < 4 && cur && cur !== document.body; i++) {
      var tag = cur.tagName.toLowerCase();
      var sid = stableId(cur);
      out.unshift(
        sid ? tag + '[data-superchat-id="' + sid + '"]' : cur.id ? tag + "#" + cur.id : tag,
      );
      cur = cur.parentElement;
    }
    return out.join(" > ").slice(0, 300);
  }
  // Text can be typed in place only where the file can take it back exactly.
  function canType(el) {
    return (
      isLeaf(el) && kindOf(el) !== "slide" && kindOf(el) !== "image" && (!PAGE || !!stableId(el))
    );
  }
  function isTarget(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el === document.body || el === document.documentElement) return false;
    if (PAGE) {
      return (
        !inLayer(el) &&
        !NOT_TARGET.test(el.tagName) &&
        document.body.contains(el) &&
        idOf(el).length <= 120
      );
    }
    if (!el.hasAttribute("data-superchat-id")) return false;
    var slide = slideOf(el);
    return !!slide && slide.classList.contains("active");
  }
  function closestTarget(node) {
    var el = node && node.nodeType === 1 ? node : node && node.parentElement;
    if (PAGE) {
      if (!el || inLayer(el)) return null;
      while (
        el &&
        el.parentElement &&
        el.parentElement !== document.body &&
        !stableId(el) &&
        (INLINE.test(el.tagName) || el.ownerSVGElement || !isTarget(el))
      )
        el = el.parentElement;
      return isTarget(el) ? el : null;
    }
    while (el && el !== document.documentElement) {
      if (isTarget(el)) return el;
      el = el.parentElement;
    }
    return null;
  }
  function isVisible(el) {
    return el.isConnected && el.getClientRects().length > 0;
  }
  // The element that holds the text: `el` itself, or the end of a chain of single wrapper
  // elements (<h2><span>Title</span></h2>), when it has only text and <br> in it. Anything that
  // mixes text with styled runs has no text root, so it is styled as a box and never flattened.
  function textRoot(el) {
    var cur = el;
    for (var depth = 0; depth < 4; depth++) {
      var kids = Array.prototype.slice.call(cur.childNodes);
      var els = kids.filter((n) => n.nodeType === 1 && n.tagName !== "BR");
      var text = kids.filter((n) => n.nodeType === 3 && n.data.trim() !== "");
      if (!els.length) return text.length ? cur : null;
      if (
        els.length === 1 &&
        !text.length &&
        /^(span|strong|em|b|i|u|small|mark|code)$/i.test(els[0].tagName)
      ) {
        cur = els[0];
        continue;
      }
      return null;
    }
    return null;
  }
  function isLeaf(el) {
    return textRoot(el) !== null;
  }
  function readText(el) {
    var out = "";
    (function walk(node) {
      node.childNodes.forEach((n) => {
        if (n.nodeType === 3) out += n.data;
        else if (n.tagName === "BR") out += "\n";
        else if (n.nodeType === 1) walk(n);
      });
    })(el);
    return out;
  }
  function kindOf(el) {
    var tag = el.tagName.toLowerCase();
    if (el.classList.contains("slide") && el.tagName === "SECTION") return "slide";
    if (tag === "img") return "image";
    if (tag === "a") return "link";
    if (isLeaf(el)) return "text";
    return "box";
  }

  // ---- reading targets ----------------------------------------------------------------------
  var COMPUTED = [
    "font-family",
    "font-size",
    "font-weight",
    "font-style",
    "color",
    "text-align",
    "line-height",
    "letter-spacing",
    "background-color",
    "border-radius",
    "border-top-width",
    "border-top-style",
    "border-top-color",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "opacity",
    "width",
    "height",
    "display",
    "text-decoration-line",
  ];
  function stageScale() {
    var stage = document.getElementById("deck-stage");
    if (!stage) return { s: 1, l: 0, t: 0 };
    var r = stage.getBoundingClientRect();
    var s = r.width / (stage.offsetWidth || 1920) || 1;
    return { s: s, l: r.left, t: r.top };
  }
  function inlineOf(el) {
    var out = {};
    var st = el.style;
    for (var i = 0; i < st.length; i++) {
      var name = st[i];
      out[name] = st.getPropertyValue(name);
    }
    return out;
  }
  function slideNumber(el) {
    if (PAGE) return 1;
    var s = slideOf(el) || el;
    return Math.max(1, slides().indexOf(s) + 1);
  }
  function targetOf(el) {
    var cs = getComputedStyle(el);
    var computed = {};
    for (var i = 0; i < COMPUTED.length; i++)
      computed[COMPUTED[i]] = cs.getPropertyValue(COMPUTED[i]);
    var sc = stageScale();
    var r = el.getBoundingClientRect();
    var kind = kindOf(el);
    var target = {
      id: idOf(el),
      kind: kind,
      tag: el.tagName.toLowerCase(),
      slide: slideNumber(el),
      text: isLeaf(el) ? readText(el).slice(0, 4000) : null,
      editable: canType(el),
      rect: {
        x: Math.round((r.left - sc.l) / sc.s),
        y: Math.round((r.top - sc.t) / sc.s),
        w: Math.round(r.width / sc.s),
        h: Math.round(r.height / sc.s),
      },
      inline: inlineOf(el),
      computed: computed,
      href: el.tagName === "A" ? el.getAttribute("href") : null,
      alt: el.tagName === "IMG" ? el.getAttribute("alt") : null,
    };
    if (PAGE) target.where = whereOf(el);
    return target;
  }
  function themeOf() {
    var colors = [];
    var fonts = [];
    var seen = {};
    var root = getComputedStyle(document.documentElement);
    for (var i = 0; i < document.styleSheets.length; i++) {
      var rules;
      try {
        rules = document.styleSheets[i].cssRules;
      } catch (e) {
        continue;
      }
      for (var j = 0; j < rules.length; j++) {
        var rule = rules[j];
        if (!rule.style || rule.selectorText !== ":root") continue;
        for (var k = 0; k < rule.style.length; k++) {
          var name = rule.style[k];
          // --shell is the framework's own letterbox colour, not part of the deck's palette
          if (name.indexOf("--") !== 0 || seen[name] || name === "--shell") continue;
          seen[name] = true;
          var value = root.getPropertyValue(name).trim();
          if (/^--font-/.test(name)) {
            var first = value.split(",")[0].replace(/^["'\s]+|["'\s]+$/g, "");
            if (!fonts.some((f) => f.label === first))
              fonts.push({ name: name, value: value, label: first });
          } else if (/^(#|rgb|hsl)/i.test(value)) {
            colors.push({ name: name, value: value });
          }
        }
      }
    }
    return {
      colors: colors.slice(0, 64),
      fonts: fonts.slice(0, 16),
      slideCount: PAGE ? 0 : slides().length,
    };
  }

  // ---- overlay ------------------------------------------------------------------------------
  function ensureLayer() {
    if (layer && layer.isConnected) return layer;
    layer = document.createElement("div");
    layer.setAttribute("data-superchat-edit-layer", "");
    layer.style.cssText =
      "position:fixed;inset:0;pointer-events:none;z-index:2147483000;font:600 11px/1 -apple-system,system-ui,sans-serif";
    document.body.appendChild(layer);
    return layer;
  }
  function box(rect, opts) {
    var d = document.createElement("div");
    d.style.cssText =
      "position:absolute;box-sizing:border-box;left:" +
      rect.left +
      "px;top:" +
      rect.top +
      "px;width:" +
      rect.width +
      "px;height:" +
      rect.height +
      "px;" +
      opts.css;
    return d;
  }
  function labelOf(el) {
    var t = kindOf(el);
    var word = t === "slide" ? "Slide " + slideNumber(el) : el.tagName.toLowerCase();
    var name = stableId(el);
    if (!name) {
      // innerText keeps the breaks between blocks: "Arches Horseshoe…", not "ArchesHorseshoe…".
      name = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 24);
      if (!name) return word;
    }
    return word + " · " + name;
  }
  // Where the label chip goes: above the selection, else below it, at its left or right end, the
  // first spot that stays on screen and covers no other text of the slide.
  var chipInset = 0;
  function chipSpot(rect, el) {
    var h = 26;
    var w = 230;
    var others = [];
    var slide = slideOf(el);
    if (slide) {
      slide.querySelectorAll("[data-superchat-id]").forEach((o) => {
        if (o === el || o.contains(el) || el.contains(o) || !isLeaf(o) || !isVisible(o)) return;
        others.push(o.getBoundingClientRect());
      });
    }
    // Keep clear of the slide rail's drawer, which opens over the stage's left edge (the host
    // says how far in).
    var left = Math.max(chipInset, rect.left);
    var right = Math.max(0, Math.min(rect.right - w, window.innerWidth - w));
    var spots = [
      { left: left, top: rect.top - h },
      { left: right, top: rect.top - h },
      { left: left, top: rect.bottom + 4 },
      { left: right, top: rect.bottom + 4 },
    ];
    var best = null;
    var fewest = 1e9;
    for (var i = 0; i < spots.length; i++) {
      var s = spots[i];
      if (s.top < 0 || s.top + h > window.innerHeight) continue;
      var hits = 0;
      for (var j = 0; j < others.length; j++) {
        var o = others[j];
        if (s.left < o.right && s.left + w > o.left && s.top < o.bottom && s.top + h > o.top)
          hits++;
      }
      if (hits < fewest) {
        fewest = hits;
        best = s;
      }
      if (!hits) break;
    }
    return best || { left: left, top: Math.max(0, rect.top - h) };
  }
  function chip(rect, text, el) {
    var c = document.createElement("div");
    var spot = chipSpot(rect, el);
    c.textContent = text;
    c.style.cssText =
      "position:absolute;max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" +
      "left:" +
      spot.left +
      "px;top:" +
      spot.top +
      "px;padding:3px 8px;border-radius:6px;background:#0a84ff;color:#fff;" +
      "box-shadow:0 1px 4px rgba(0,0,0,.25)";
    return c;
  }
  function paint() {
    raf = 0;
    var l = ensureLayer();
    var list = [];
    var keep = selected.filter(
      (el) => isVisible(el) && (PAGE || (slideOf(el) && slideOf(el).classList.contains("active"))),
    );
    if (keep.length !== selected.length) {
      selected = keep;
      postSelection();
    }
    var sig = [];
    selected.forEach((el, i) => {
      var r = el.getBoundingClientRect();
      sig.push(r.left + "," + r.top + "," + r.width + "," + r.height);
      list.push({ el: el, r: r, primary: i === selected.length - 1 });
    });
    var h = hovered && isVisible(hovered) && selected.indexOf(hovered) < 0 ? hovered : null;
    var hr = h ? h.getBoundingClientRect() : null;
    if (hr) sig.push("h" + hr.left + "," + hr.top + "," + hr.width + "," + hr.height);
    sig.push(editing ? "e" : "");
    var key = sig.join("|");
    if (key !== lastRects) {
      lastRects = key;
      l.textContent = "";
      if (hr)
        l.appendChild(
          box(hr, { css: "outline:1.5px solid rgba(10,132,255,.7);outline-offset:-1px" }),
        );
      list.forEach((item) => {
        var css =
          editing && editing.el === item.el
            ? "outline:2px dashed #0a84ff;outline-offset:2px"
            : "outline:2px solid #0a84ff;outline-offset:-1px;background:rgba(10,132,255,.06)";
        l.appendChild(box(item.r, { css: css }));
        if (item.primary && !editing) l.appendChild(chip(item.r, labelOf(item.el), item.el));
      });
    }
    if (selected.length || h || editing) schedule();
  }
  function schedule() {
    if (!raf) raf = requestAnimationFrame(paint);
  }
  function repaint() {
    lastRects = "";
    schedule();
  }

  // ---- selection ----------------------------------------------------------------------------
  function postSelection() {
    send({ type: "superchat:edit-selection", targets: selected.map(targetOf) });
  }
  function setSelection(list, silent) {
    selected = list.filter(Boolean);
    if (!silent) postSelection();
    repaint();
  }
  function selectIds(ids) {
    var els = [];
    ids.forEach((id) => {
      var el = findById(id);
      if (el && isTarget(el)) els.push(el);
    });
    setSelection(els, false);
  }

  // ---- inline text edit ---------------------------------------------------------------------
  function caretFromPoint(x, y) {
    try {
      if (document.caretPositionFromPoint) {
        var p = document.caretPositionFromPoint(x, y);
        if (!p) return null;
        var r = document.createRange();
        r.setStart(p.offsetNode, p.offset);
        r.collapse(true);
        return r;
      }
      if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
    } catch (e) {}
    return null;
  }
  function startEdit(el, point) {
    if (editing && editing.el === el) return;
    if (editing) finishEdit(true);
    if (!isLeaf(el)) return;
    var before = readText(el);
    el.contentEditable = "plaintext-only";
    if (el.contentEditable !== "plaintext-only") el.contentEditable = "true";
    el.spellcheck = false;
    editing = { el: el, before: before, beforeHtml: el.innerHTML };
    try {
      el.focus({ preventScroll: true });
    } catch (e) {}
    var range = point ? caretFromPoint(point.x, point.y) : null;
    if (!range || !el.contains(range.startContainer)) {
      range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
    }
    try {
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
    send({ type: "superchat:edit-text-session", id: idOf(el), active: true });
    repaint();
  }
  function finishEdit(commit) {
    if (!editing) return false;
    var s = editing;
    editing = null;
    var el = s.el;
    el.removeAttribute("contenteditable");
    el.removeAttribute("spellcheck");
    var text = readText(el).replace(/\n+$/g, "");
    if (!commit) el.innerHTML = s.beforeHtml;
    else if (text !== s.before) {
      send({
        type: "superchat:edit-text-commit",
        id: idOf(el),
        text: text,
        before: s.before,
      });
    }
    send({ type: "superchat:edit-text-session", id: idOf(el), active: false });
    try {
      window.getSelection().removeAllRanges();
      document.body.focus({ preventScroll: true });
    } catch (e) {}
    postSelection();
    repaint();
    return true;
  }

  // ---- events (capture, ahead of the deck's own handlers) -----------------------------------
  function stop(e, prevent) {
    e.stopImmediatePropagation();
    if (prevent) e.preventDefault();
  }
  function onClick(e) {
    if (e.button !== undefined && e.button !== 0) return;
    if (editing && editing.el.contains(e.target)) return stop(e, false);
    stop(e, true);
    var el = closestTarget(e.target);
    if (editing) finishEdit(true);
    if (!el) return setSelection([], false);
    if (e.shiftKey) {
      var at = selected.indexOf(el);
      var next = selected.slice();
      if (at >= 0) next.splice(at, 1);
      else next.push(el);
      return setSelection(next, false);
    }
    if (selected.length === 1 && selected[0] === el) return;
    setSelection([el], false);
  }
  function onDblClick(e) {
    var el = closestTarget(e.target);
    if (editing && editing.el.contains(e.target)) return stop(e, false);
    stop(e, true);
    if (!el) return;
    if (selected.length !== 1 || selected[0] !== el) setSelection([el], false);
    if (canType(el)) startEdit(el, { x: e.clientX, y: e.clientY });
  }
  function swallow(e) {
    if (editing && editing.el.contains(e.target)) return;
    // The deck refocuses itself on mousedown and navigates on click; neither may run in edit mode.
    e.stopImmediatePropagation();
    if (e.type === "mousedown" && closestTarget(e.target)) e.preventDefault();
  }
  function onMove(e) {
    var el = editing ? null : closestTarget(e.target);
    if (el !== hovered) {
      hovered = el;
      repaint();
    }
  }
  function onLeave() {
    hovered = null;
    repaint();
  }
  function onKey(e) {
    var mod = e.metaKey || e.ctrlKey;
    if (editing) {
      if (e.key === "Enter" && !e.shiftKey) {
        stop(e, true);
        finishEdit(true);
      } else if (e.key === "Enter") {
        stop(e, true);
      } else if (e.key === "Escape") {
        stop(e, true);
        finishEdit(false);
      } else if (mod && e.key.toLowerCase() === "z") {
        // The browser's own text undo handles it inside the field.
        e.stopImmediatePropagation();
      } else {
        e.stopImmediatePropagation();
      }
      return;
    }
    var k = e.key;
    if (mod && k.toLowerCase() === "z") {
      stop(e, true);
      return send({ type: "superchat:edit-key", action: e.shiftKey ? "redo" : "undo" });
    }
    if (mod && k.toLowerCase() === "y") {
      stop(e, true);
      return send({ type: "superchat:edit-key", action: "redo" });
    }
    if (!selected.length) return; // no selection: the deck's own keys (slide navigation) apply
    if (k === "Escape") {
      stop(e, true);
      return setSelection([], false);
    }
    if (k === "Enter" && selected.length === 1 && canType(selected[0])) {
      stop(e, true);
      return startEdit(selected[0], null);
    }
    if (k === "Delete" || k === "Backspace") {
      stop(e, true);
      return send({ type: "superchat:edit-key", action: "delete" });
    }
    if (mod && k.toLowerCase() === "d") {
      stop(e, true);
      return send({ type: "superchat:edit-key", action: "duplicate" });
    }
    var step = e.shiftKey ? 10 : 1;
    var arrows = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    if (arrows[k] && !mod && selected.every((el) => kindOf(el) !== "slide")) {
      stop(e, true);
      send({ type: "superchat:edit-key", action: "nudge", dx: arrows[k][0], dy: arrows[k][1] });
    }
  }

  // ---- host messages ------------------------------------------------------------------------
  function onMessage(e) {
    if (e.source !== window.parent) return;
    var d = e.data;
    if (!d || typeof d !== "object" || d.nonce !== NONCE || d.v !== V) return;
    if (d.type === "superchat:edit-select" && Array.isArray(d.ids)) {
      if (editing) finishEdit(true);
      selectIds(d.ids);
      var first = d.scroll && d.ids[0] && findById(d.ids[0]);
      if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
    } else if (d.type === "superchat:edit-preview" && typeof d.id === "string" && d.style) {
      var el = findById(d.id);
      if (!el) return;
      Object.keys(d.style).forEach((prop) => {
        if (!/^[a-z-]{1,64}$/.test(prop)) return;
        var value = d.style[prop];
        if (value === null) el.style.removeProperty(prop);
        else el.style.setProperty(prop, String(value));
      });
      repaint();
      postSelection();
    } else if (d.type === "superchat:edit-chip-inset" && typeof d.left === "number") {
      chipInset = d.left >= 0 && d.left <= 400 ? d.left : 0;
      repaint();
    } else if (d.type === "superchat:edit-text-finish") {
      finishEdit(d.commit !== false);
    }
  }

  function boot() {
    window.addEventListener("click", onClick, true);
    window.addEventListener("dblclick", onDblClick, true);
    window.addEventListener("mousedown", swallow, true);
    window.addEventListener("mouseup", swallow, true);
    window.addEventListener("pointerdown", swallow, true);
    window.addEventListener("mousemove", onMove, true);
    document.addEventListener("mouseleave", onLeave);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("message", onMessage);
    window.addEventListener("resize", repaint);
    // A link must never navigate in edit mode.
    document.addEventListener(
      "submit",
      (e) => {
        e.preventDefault();
      },
      true,
    );
    var style = document.createElement("style");
    style.setAttribute("data-superchat-edit-style", "");
    style.textContent =
      "[contenteditable]{outline:none;caret-color:#0a84ff;cursor:text}body{cursor:default}";
    document.head.appendChild(style);
    send({ type: "superchat:edit-ready", theme: themeOf() });
  }
  if (document.readyState === "complete") boot();
  else window.addEventListener("load", boot);
})();
