// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

const api = vi.hoisted(() => ({ getById: vi.fn(), listVersions: vi.fn() }));
vi.mock("../../../lib/rpc", () => ({ rpc: { artifacts: api } }));
vi.mock("@superchat/chat-ui/web", () => ({
  ChatMarkdown: ({ children }: { children?: ReactNode }) => <div data-testid="md">{children}</div>,
}));
vi.mock("../../../components/SandboxedHtmlViewer", () => ({
  SandboxedHtmlViewer: ({ html }: { html: string }) => (
    <iframe data-testid="html" srcDoc={html} title="p" />
  ),
}));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({ t: (parts: TemplateStringsArray) => parts.join("") }),
}));

import { ArtifactInlinePreview, previewKind } from "./ArtifactInlinePreview";

beforeEach(() => {
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(private cb: (entries: Array<{ isIntersecting: boolean }>) => void) {}
      observe() {
        this.cb([{ isIntersecting: true }]);
      }
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  api.getById.mockReset();
});

const b64 = (text: string) => btoa(text);

async function render(props: { artifactId: string; name: string }) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<ArtifactInlinePreview {...props} />));
  return container;
}

it("picks a preview kind by mime type", () => {
  expect(previewKind("text/html")).toBe("html");
  expect(previewKind("text/markdown")).toBe("markdown");
  expect(previewKind("text/csv")).toBe("csv");
  expect(previewKind("application/pdf")).toBe("pdf");
  expect(previewKind("image/png")).toBe("image");
  expect(
    previewKind("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
  ).toBe("cover");
});

it("renders html, csv and markdown previews from the artifact bytes", async () => {
  api.getById.mockResolvedValueOnce({ mimeType: "text/html", contentBase64: b64("<h1>x</h1>") });
  let c = await render({ artifactId: "a1", name: "p.html" });
  expect(c.querySelector("[data-testid=artifact-preview]")?.getAttribute("data-preview")).toBe(
    "html",
  );
  expect(c.querySelector("[data-testid=html]")).not.toBeNull();

  api.getById.mockResolvedValueOnce({ mimeType: "text/csv", contentBase64: b64("a,b\n1,2\n3,4") });
  c = await render({ artifactId: "a2", name: "t.csv" });
  expect(c.querySelectorAll("tbody tr")).toHaveLength(2);

  api.getById.mockResolvedValueOnce({ mimeType: "text/markdown", contentBase64: b64("# hi") });
  c = await render({ artifactId: "a3", name: "n.md" });
  expect(c.querySelector("[data-testid=md]")?.textContent).toBe("# hi");
});

it("falls back to the cover when the preview fails to load", async () => {
  api.getById.mockRejectedValueOnce(new Error("boom"));
  const c = await render({ artifactId: "a4", name: "report.docx" });
  expect(c.querySelector("[data-testid=artifact-preview]")?.getAttribute("data-preview")).toBe(
    "cover",
  );
  expect(c.textContent).toContain("report.docx");
});

it("shows a cover for office documents", async () => {
  api.getById.mockResolvedValueOnce({
    mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    contentBase64: b64("x"),
  });
  const c = await render({ artifactId: "a5", name: "deck.pptx" });
  expect(c.textContent).toContain("deck.pptx");
  expect(c.querySelector("iframe")).toBeNull();
});

it("follows a deck that was still being saved when the card first rendered", async () => {
  vi.useFakeTimers();
  api.getById.mockResolvedValueOnce({ mimeType: "text/html", contentBase64: b64("") });
  api.getById.mockResolvedValue({
    mimeType: "text/html",
    contentBase64: b64('<section class="slide">hi</section>'),
  });
  api.listVersions.mockResolvedValue([{ id: "v2" }]);
  const c = await render({ artifactId: "d1", name: "x.deck.html" });
  const preview = () => c.querySelector("[data-testid=artifact-preview]");
  expect(preview()?.getAttribute("data-preview")).toBe("cover");
  expect(c.querySelector("[data-testid=html]")).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3100);
  });
  expect(preview()?.getAttribute("data-preview")).toBe("html");
  expect(c.querySelector("[data-testid=html]")).not.toBeNull();
  vi.useRealTimers();
});
