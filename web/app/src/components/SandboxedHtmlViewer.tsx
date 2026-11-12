import type { Ref } from "react";

const PREVIEW_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'";

// frame-src about: allows srcdoc and rejects navigations the sandbox and inner CSP do not block.
// The srcdoc preview inherits this policy on top of its own, so the shell must allow the same
// embedded (data:) images and fonts, or a deck's embedded fonts are blocked.
const SHELL_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-src about:; child-src 'none'; form-action 'none'; base-uri 'none'";

const PREVIEW_GUARD = `<script>
document.addEventListener("click", (event) => {
  const target = event.target;
  const link = target instanceof Element ? target.closest("a[href]") : null;
  if (!link) return;
  const href = link.getAttribute("href") ?? "";
  event.preventDefault();
  if (href.startsWith("#")) location.hash = href;
}, true);
document.addEventListener("submit", (event) => event.preventDefault(), true);
</script>`;

/** JSON-embed so user HTML cannot close the shell's script and run in it. */
function embedScriptString(value: string): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

// A thumbnail is only a picture: its document must not take the keyboard from the real one when
// it finishes loading (a deck focuses itself so the arrow keys work).
const NO_FOCUS = `<script>window.focus=function(){};HTMLElement.prototype.focus=function(){};</script>`;

function withPreviewDocument(html: string, decorative: boolean): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">`;
  const referrer = `<meta name="referrer" content="no-referrer">`;
  return `${meta}${referrer}${PREVIEW_GUARD}${decorative ? NO_FOCUS : ""}${html}`;
}

// Opt-in relay for a document that talks to its host (a deck): the preview is nested one frame
// deep, so its messages would stop at this shell. Only `superchat:`-typed messages cross, in both
// directions, and only from the preview or the host.
const RELAY_SCRIPT = `<script>
(function () {
  var preview = document.getElementById("preview");
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object" || typeof data.type !== "string") return;
    if (data.type.indexOf("superchat:") !== 0) return;
    if (event.source === preview.contentWindow) window.parent.postMessage(data, "*");
    else if (event.source === window.parent) preview.contentWindow.postMessage(data, "*");
  });
})();
</script>`;

function shellDocument(innerHtml: string, relay: boolean): string {
  const payload = embedScriptString(innerHtml);
  return `<!DOCTYPE html><meta http-equiv="Content-Security-Policy" content="${SHELL_CSP}"><meta name="referrer" content="no-referrer"><style>html,body{height:100%;margin:0}iframe{width:100%;height:100%;border:0;background:#fff}</style><iframe id="preview" sandbox="allow-scripts" referrerpolicy="no-referrer" csp="${PREVIEW_CSP}"></iframe><script>document.getElementById("preview").srcdoc=${payload};</script>${relay ? RELAY_SCRIPT : ""}`;
}

export function SandboxedHtmlViewer({
  html,
  title,
  relay = false,
  decorative = false,
  frameRef,
}: {
  html: string;
  title: string;
  /** Pass `superchat:` messages between the host and the document (decks). */
  relay?: boolean;
  /** A picture of the document (a thumbnail): it may not take focus. */
  decorative?: boolean;
  /** The outer frame, so the host can post to and recognise its window. */
  frameRef?: Ref<HTMLIFrameElement>;
}) {
  return (
    <iframe
      ref={frameRef}
      title={title}
      srcDoc={shellDocument(withPreviewDocument(html, decorative), relay)}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      className="h-full w-full border-0 bg-white"
    />
  );
}
