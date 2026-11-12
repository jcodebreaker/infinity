/** A design token's current color, resolved (the preview frame cannot read the app's CSS). */
export function tokenColor(token: string): string {
  const probe = document.createElement("span");
  probe.style.color = `var(${token})`;
  document.body.append(probe);
  const color = getComputedStyle(probe).color;
  probe.remove();
  return color;
}

/** The page with a quiet underline and tint on the parts with these `data-superchat-id`s. */
export function markParts(html: string, ids: readonly string[], color: string): string {
  if (!ids.length || !color) return html;
  const selector = ids.map((id) => `[data-superchat-id="${CSS.escape(id)}"]`).join(",");
  const rule = `${selector}{background:color-mix(in srgb,${color} 14%,transparent);text-decoration:underline ${color};text-decoration-thickness:2px;text-underline-offset:3px}`;
  const style = `<style data-superchat-marked>${rule}</style>`;
  const head = /<head\b[^>]*>/i.exec(html);
  if (!head) return style + html;
  const at = head.index + head[0].length;
  return html.slice(0, at) + style + html.slice(at);
}
