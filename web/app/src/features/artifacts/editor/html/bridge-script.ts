import bridgeSource from "./bridge.js?raw";

/** A fresh nonce for one edit frame; the bridge only answers messages that carry it. */
export function makeEditNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** What the bridge selects: `deck` — the active slide's `data-superchat-id` elements; `page` —
 * any element of the document (one without an id gets a `~` path id, good for asking only). */
export type BridgeMode = "deck" | "page";

/**
 * The file with the edit bridge as the first script of `<head>`, so its capture listeners run
 * before the file's own click and key handlers. Only the copy handed to the viewer changes; the
 * source file never contains the bridge.
 */
export function injectEditBridge(html: string, nonce: string, mode: BridgeMode = "deck"): string {
  const source = bridgeSource
    .split("__SUPERCHAT_NONCE__")
    .join(nonce)
    .split("__SUPERCHAT_MODE__")
    .join(mode);
  const script = `<script data-superchat-edit-bridge>${source}</script>`;
  const head = /<head\b[^>]*>/i.exec(html);
  if (head)
    return (
      html.slice(0, head.index + head[0].length) + script + html.slice(head.index + head[0].length)
    );
  return script + html;
}
