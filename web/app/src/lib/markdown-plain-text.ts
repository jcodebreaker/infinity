const MARKDOWN_LINK = /\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g;
const BARE_URL = /https?:\/\/[^\s)\]>]+/g;

/** Markdown reduced to plain text for previews: the first heading (if the text opens with
 * one) apart from the body, and everything else with syntax, links and URLs removed. */
export function markdownToPlainText(markdown: string): { heading: string | null; text: string } {
  const inline = (line: string) =>
    line
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(MARKDOWN_LINK, "$1")
      .replace(BARE_URL, "")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/(\*\*|__|\*|_|~~)(.+?)\1/g, "$2")
      .replace(/\s+/g, " ")
      .trim();

  let heading: string | null = null;
  const lines: string[] = [];
  for (const raw of markdown.split("\n")) {
    const headingMatch = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/.exec(raw);
    if (headingMatch) {
      const text = inline(headingMatch[1] ?? "");
      if (heading === null && lines.length === 0) heading = text || null;
      else if (text) lines.push(text);
      continue;
    }
    const text = inline(
      raw
        .replace(/^\s*(```|~~~).*$/, "")
        .replace(/^\s*>+\s?/, "")
        .replace(/^\s*([-*+]|\d+[.)])\s+/, "")
        .replace(/^\s*([-*_]\s*){3,}$/, ""),
    );
    if (text) lines.push(text);
  }
  return { heading, text: lines.join(" ") };
}

/** Markdown as one plain line of text, heading first — for list snippets. */
export function markdownToLine(markdown: string): string {
  const { heading, text } = markdownToPlainText(markdown);
  return [heading, text].filter(Boolean).join(" ");
}
