// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getById: vi.fn(),
  listVersions: vi.fn(),
  publish: vi.fn(),
  unpublish: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({
  rpc: {
    artifacts: { getById: api.getById, listVersions: api.listVersions },
    apps: { publish: api.publish, unpublish: api.unpublish },
  },
}));
vi.mock("../artifacts/ArtifactPreview", () => ({
  ArtifactPreview: ({ bytes }: { bytes: Uint8Array }) => (
    <div data-testid="body">{new TextDecoder().decode(bytes)}</div>
  ),
}));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));
vi.mock("@lingui/core/macro", () => ({
  t: (parts: TemplateStringsArray) => parts.join(""),
  plural: (n: number, forms: { one: string; other: string }) =>
    (n === 1 ? forms.one : forms.other).replace("#", String(n)),
}));

import { ArtifactRegistryProvider } from "../artifacts";
import { ArtifactPreviewDialog } from "../artifacts/library/ArtifactPreviewDialog";
import { appsExtension } from "./extension";

i18n.loadAndActivate({ locale: "en", messages: {} });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  api.getById.mockReset();
  api.listVersions.mockReset();
  api.publish.mockReset();
  api.unpublish.mockReset();
  document.body.innerHTML = "";
});

const b64 = (text: string) => btoa(text);
const tick = () => act(async () => {});

async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <ArtifactRegistryProvider value={[appsExtension]}>{node}</ArtifactRegistryProvider>
      </I18nProvider>,
    ),
  );
  return container;
}

const libraryArtifact = (mimeType: string, name: string, publish: unknown = null) => ({
  id: "a1",
  name,
  mimeType,
  version: 3,
  contentBase64: b64("x"),
  publish,
});
const dialogButtons = () => [...document.querySelectorAll<HTMLElement>("button")];
const dialogByText = (text: string) =>
  dialogButtons().find((b) => b.textContent?.trim() === text || b.ariaLabel === text);

it("the file dialog offers Edit, Publish and Download for an HTML artifact", async () => {
  api.listVersions.mockResolvedValue([{ id: "a1", version: 3, name: "dog.html", createdAt: "" }]);
  api.getById.mockResolvedValue(libraryArtifact("text/html", "dog.html"));
  await mount(<ArtifactPreviewDialog artifactId="a1" onOpenChange={() => {}} />);
  await tick();
  expect(dialogByText("Publish…")).toBeTruthy();
  expect(dialogByText("Edit")).toBeTruthy();
  expect(dialogByText("Download")).toBeTruthy();
});

it("the file dialog shows the Published bar for a published app", async () => {
  api.listVersions.mockResolvedValue([]);
  api.getById.mockResolvedValue(
    libraryArtifact("text/html", "dog.html", {
      slug: "dog-k3m9xq",
      urlPath: "/apps/dog-k3m9xq",
      audience: "link",
      version: 3,
      publishedAt: "",
      stats: { opensTotal: 14, uniqueViewers: 5, opens7d: 9 },
    }),
  );
  await mount(<ArtifactPreviewDialog artifactId="a1" onOpenChange={() => {}} />);
  await tick();
  expect(document.querySelector("[data-testid=published-bar]")?.textContent).toContain("14 opens");
  expect(dialogByText("Publish settings")).toBeTruthy();
});

it("the file dialog offers no Publish for a non-HTML artifact", async () => {
  api.listVersions.mockResolvedValue([]);
  api.getById.mockResolvedValue(libraryArtifact("text/markdown", "n.md"));
  await mount(<ArtifactPreviewDialog artifactId="a1" onOpenChange={() => {}} />);
  await tick();
  expect(dialogByText("Download")).toBeTruthy();
  expect(dialogByText("Publish…")).toBeUndefined();
  expect(dialogByText("Edit")).toBeUndefined();
});
