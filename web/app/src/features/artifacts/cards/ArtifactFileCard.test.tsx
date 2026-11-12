// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

const list = vi.fn();
vi.mock("../../../lib/rpc", () => ({ rpc: { artifacts: { list: (a: unknown) => list(a) } } }));
vi.mock("./ArtifactInlinePreview", async () => {
  const { useEffect } = await import("react");
  return {
    ArtifactInlinePreview: ({
      artifactId,
      onText,
    }: {
      artifactId: string;
      onText?: (text: string) => void;
    }) => {
      // The file arrives: a deck of three slides.
      useEffect(() => {
        onText?.('<section class="slide a"></section>'.repeat(3));
      }, [onText]);
      return <div data-preview={artifactId} />;
    },
  };
});
vi.mock("../../../components/cards/catalog", () => ({
  Frame: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  formatSize: () => "1 KB",
  catalog: { file: () => <div data-plain-file /> },
}));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({ t: (p: TemplateStringsArray) => p.join("") }),
}));
vi.mock("@superchat/ui-web", () => {
  const P = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    Button: P,
    DropdownMenu: P,
    DropdownMenuContent: P,
    DropdownMenuItem: ({
      children,
      onClick,
      "data-testid": testId,
    }: {
      children?: ReactNode;
      onClick?: () => void;
      "data-testid"?: string;
    }) => (
      <button type="button" data-testid={testId} onClick={onClick}>
        {children}
      </button>
    ),
    DropdownMenuTrigger: P,
  };
});

import { FileStudioProvider, ReplyCardBotProvider } from "../../../components/cards/context";
import { readDeckThemeOpen, resetDeckUi } from "../../decks/deck-ui-state";
import { ArtifactFileCard } from "./ArtifactFileCard";

const hosts: HTMLElement[] = [];
afterEach(() => {
  for (const host of hosts.splice(0)) host.remove();
  list.mockReset();
});

function render(data: {
  name: string;
  artifactId?: string;
  kind?: string;
  size?: number;
}): HTMLElement {
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  act(() => createRoot(host).render(<ArtifactFileCard data={data} />));
  return host;
}

it("previews a card by its artifact id without listing deliverables", () => {
  const host = render({ name: "plan.html", artifactId: "art-9" });
  expect(host.querySelector("[data-preview]")?.getAttribute("data-preview")).toBe("art-9");
  expect(list).not.toHaveBeenCalled();
});

it("keeps the plain card, with no lookup, when the card has no artifact id", () => {
  const host = render({ name: "none.html" });
  expect(host.querySelector("[data-plain-file]")).not.toBeNull();
  expect(list).not.toHaveBeenCalled();
});

it("labels a deck as a deck with its slide count, not as an HTML file", () => {
  const host = render({ name: "q3.deck.html", artifactId: "art-1", kind: "html", size: 432_000 });
  expect(host.textContent).toContain("Deck · 3 slides");
  expect(host.textContent).not.toContain("HTML");
  const page = render({ name: "plan.html", artifactId: "art-2", kind: "html", size: 1000 });
  expect(page.textContent).toContain("HTML · 1 KB");
});

it("marks a draft a Helper is still writing as Updating, in place of its version", () => {
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  const data = { name: "q3.deck.html", artifactId: "art-1", version: 3, versions: 3 };
  act(() => createRoot(host).render(<ArtifactFileCard data={data} updating />));
  expect(host.querySelector("[data-testid=artifact-updating]")?.textContent).toBe("Updating");
  expect(host.querySelector("[data-testid=artifact-version]")).toBeNull();
  const done = render(data);
  expect(done.querySelector("[data-testid=artifact-updating]")).toBeNull();
  expect(done.querySelector("[data-testid=artifact-version]")?.textContent).toBe("v3");
});

it("a deck's card offers Change look: the panel opens on the Theme gallery (ADR 0025)", () => {
  const open = vi.fn();
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  act(() =>
    createRoot(host).render(
      <FileStudioProvider value={{ open }}>
        <ReplyCardBotProvider botId="bot-1">
          <ArtifactFileCard data={{ name: "q3.deck.html", artifactId: "art-1" }} title="Q3" />
        </ReplyCardBotProvider>
      </FileStudioProvider>,
    ),
  );
  act(() => (host.querySelector('[data-testid="deck-change-look"]') as HTMLElement).click());
  expect(open).toHaveBeenCalledWith("art-1", "Q3");
  expect(readDeckThemeOpen("bot-1:q3.deck.html")).toBe(true);
  resetDeckUi();
});

it("only a deck offers Change look", () => {
  const host = render({ name: "plan.html", artifactId: "art-9" });
  expect(host.querySelector('[data-testid="deck-change-look"]')).toBeNull();
});
