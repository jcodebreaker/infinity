// @vitest-environment jsdom

import type { ReplyCardBlock } from "@superchat/contracts";
import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return { useLingui: () => ({ t, i18n: { locale: "en" } }) };
});
vi.mock("@superchat/chat-ui/web", () => ({
  ChatMarkdown: ({ children }: { children?: ReactNode }) => <div data-md>{children}</div>,
}));
vi.mock("@superchat/ui-web", () => {
  const Passthrough = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
    Badge: Passthrough,
    Card: ({ children }: { children?: ReactNode }) => (
      <div data-testid="reply-card">{children}</div>
    ),
    CardAction: Passthrough,
    CardContent: Passthrough,
    CardHeader: Passthrough,
    CardTitle: Passthrough,
    Checkbox: () => <input type="checkbox" readOnly />,
    Skeleton: () => <i data-skeleton />,
    Button: ({ render, ...props }: ComponentProps<"button"> & { render?: ReactNode }) =>
      render ? <a {...(props as object)} /> : <button type="button" {...props} />,
  };
});

import {
  type AskPreviewProps,
  AskPreviewsProvider,
  ReplyCardSendProvider,
  ReplyCardThreadProvider,
} from "./context";
import { ReplyCard, ReplyCardBlockView } from "./ReplyCard";

const mounted: HTMLElement[] = [];
function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  mounted.push(host);
  act(() => createRoot(host).render(node));
  return host;
}
afterEach(() => {
  for (const host of mounted.splice(0)) host.remove();
});

const block = (over: Partial<ReplyCardBlock>): ReplyCardBlock => ({
  kind: "reply_card",
  card: "sources",
  data: {},
  fallback: "**Fallback** text",
  ...over,
});

it("draws the fallback markdown for an unknown kind or malformed data", () => {
  const unknown = mount(<ReplyCard block={block({ card: "hologram" })} />);
  expect(unknown.querySelector("[data-md]")?.textContent).toBe("**Fallback** text");
  // An older chat's plan card is no kind any more (ADR 0029).
  const plan = mount(<ReplyCard block={block({ card: "plan", data: { items: [] } })} />);
  expect(plan.querySelector("[data-md]")?.textContent).toBe("**Fallback** text");
  const malformed = mount(<ReplyCard block={block({ card: "quote", data: { symbol: "A" } })} />);
  expect(malformed.querySelector("[data-md]")).not.toBeNull();
  expect(malformed.querySelector("[data-testid=reply-card]")).toBeNull();
});

it("shows a skeleton while pending", () => {
  const host = mount(<ReplyCard block={block({ pending: true })} />);
  expect(host.querySelector("[data-skeleton]")).not.toBeNull();
});

it("links only http(s) and always with noopener noreferrer", () => {
  const host = mount(
    <ReplyCard
      block={block({
        data: {
          items: [
            { title: "Good", url: "https://example.com/a" },
            { title: "Bad", url: "javascript:alert(1)" },
          ],
        },
      })}
    />,
  );
  const links = [...host.querySelectorAll("a")];
  expect(links).toHaveLength(1);
  expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer");
  expect(host.textContent).toContain("Bad");
});

const ask = block({
  card: "ask",
  data: {
    question: "Which?",
    options: [
      { id: "a", label: "Berlin" },
      { id: "b", label: "Seoul" },
    ],
  },
});

it("sends the option label and locks the card", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={ask} />
    </ReplyCardSendProvider>,
  );
  const buttons = () => [...host.querySelectorAll("button")];
  act(() => buttons()[1]?.click());
  expect(send).toHaveBeenCalledWith("Seoul");
  expect(buttons()[0]?.disabled).toBe(true);
  act(() => buttons()[1]?.click());
  expect(send).toHaveBeenCalledTimes(1);
});

it("sends the picked option's id beside its label (ADR 0025)", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={ask} cardId="m1:0" />
    </ReplyCardSendProvider>,
  );
  act(() => [...host.querySelectorAll("button")][1]?.click());
  expect(send).toHaveBeenCalledExactlyOnceWith("Seoul", {
    cardId: "m1:0",
    optionId: "b",
    label: "Seoul",
  });
});

it("names the card by the engine's id in a transcript, never by its place", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCardBlockView block={{ ...ask, id: "card-7" }} messageId="m1" index={0} />
    </ReplyCardSendProvider>,
  );
  act(() => [...host.querySelectorAll("button")][0]?.click());
  expect(send).toHaveBeenCalledExactlyOnceWith("Berlin", {
    cardId: "card-7",
    optionId: "a",
    label: "Berlin",
  });
});

it("two clicks on different options in the same tick still send exactly once", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={ask} />
    </ReplyCardSendProvider>,
  );
  const [berlin, seoul] = [...host.querySelectorAll("button")] as HTMLButtonElement[];
  act(() => {
    seoul?.click();
    berlin?.click();
    seoul?.click();
  });
  expect(send).toHaveBeenCalledExactlyOnceWith("Seoul");
});

it("shows an already answered ask locked on the chosen option", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={ask} answer="Berlin" />
    </ReplyCardSendProvider>,
  );
  const [berlin, seoul] = [...host.querySelectorAll("button")];
  expect(berlin?.getAttribute("aria-pressed")).toBe("true");
  expect(seoul?.disabled).toBe(true);
});

it("draws the pages a file search found as chips", () => {
  const host = mount(
    <ReplyCard
      block={block({
        card: "passages",
        data: { items: [{ artifactId: "a".repeat(32), name: "finance.pdf", page: 2 }] },
      })}
    />,
  );
  expect(host.querySelector("[data-testid=passage-chips]")?.textContent).toContain("finance.pdf");
  expect(host.querySelector("[data-md]")).toBeNull();
});

const followUps = block({
  card: "follow_ups",
  data: { suggestions: ["Compare with last year", "Show it by region"] },
});

it("clarification: renders 2-5 option buttons and the click is the person's reply", () => {
  const send = vi.fn();
  const five = block({
    card: "ask",
    data: {
      question: "Which quarter?",
      options: ["Q1", "Q2", "Q3", "Q4", "Full year"].map((label, i) => ({
        id: `opt-${i + 1}`,
        label,
      })),
    },
  });
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={five} />
    </ReplyCardSendProvider>,
  );
  expect(host.textContent).toContain("Which quarter?");
  const buttons = [...host.querySelectorAll("button")];
  expect(buttons.map((b) => b.textContent)).toEqual(["Q1", "Q2", "Q3", "Q4", "Full year"]);
  act(() => buttons[4]?.click());
  expect(send).toHaveBeenCalledExactlyOnceWith("Full year");
  expect(buttons.every((b) => b.disabled || b.getAttribute("aria-pressed") === "true")).toBe(true);
});

it("follow-ups: chips send their text once and then lock", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={followUps} />
    </ReplyCardSendProvider>,
  );
  const chips = [
    ...host.querySelectorAll("[data-testid=follow-ups] button"),
  ] as HTMLButtonElement[];
  expect(chips.map((c) => c.textContent)).toEqual(["Compare with last year", "Show it by region"]);
  act(() => chips[1]?.click());
  expect(send).toHaveBeenCalledExactlyOnceWith("Show it by region");
  expect(chips.every((c) => c.disabled)).toBe(true);
  act(() => chips[0]?.click());
  expect(send).toHaveBeenCalledTimes(1);
});

it("follow-ups with no send path are inert and malformed data falls back to text", () => {
  const inert = mount(<ReplyCard block={followUps} />);
  expect(inert.querySelector<HTMLButtonElement>("[data-testid=follow-ups] button")?.disabled).toBe(
    true,
  );
  const bad = mount(<ReplyCard block={{ ...followUps, data: { suggestions: [] } }} />);
  expect(bad.querySelector("[data-testid=follow-ups]")).toBeNull();
  expect(bad.querySelector("[data-md]")).not.toBeNull();
});

const thread = (role: "bot" | "user", id: string, ...blocks: ReplyCardBlock[]) => ({
  id,
  threadId: "t",
  seq: Number(id.slice(1)),
  role,
  blocks: blocks as ReplyCardBlock[],
  createdAt: "2026-10-09T10:00:00.000Z",
});

function viewOf(messages: ReturnType<typeof thread>[], running = false) {
  return mount(
    <ReplyCardThreadProvider messages={messages} running={running}>
      <ReplyCardSendProvider send={vi.fn()}>
        {messages.map((m) =>
          m.blocks.map((b, i) => (
            <ReplyCardBlockView
              key={`${m.id}${i}`}
              block={b as ReplyCardBlock}
              messageId={m.id}
              index={i}
            />
          )),
        )}
      </ReplyCardSendProvider>
    </ReplyCardThreadProvider>,
  );
}

it("follow-ups show on the latest answer only", () => {
  const old = thread("bot", "m1", followUps);
  const latest = thread("bot", "m3", followUps);
  const reply = thread("user", "m2");
  expect(viewOf([old, reply, latest]).querySelectorAll("[data-testid=follow-ups]")).toHaveLength(1);
  // The person already replied: the earlier chips are gone.
  expect(viewOf([old, reply]).querySelectorAll("[data-testid=follow-ups]")).toHaveLength(0);
});

it("follow-ups are hidden while the Muse is working, and outside a resolved thread", () => {
  const only = thread("bot", "m1", followUps);
  expect(viewOf([only], true).querySelector("[data-testid=follow-ups]")).toBeNull();
  expect(viewOf([only], false).querySelector("[data-testid=follow-ups]")).not.toBeNull();
  const bare = mount(<ReplyCardBlockView block={followUps} messageId="m1" index={0} />);
  expect(bare.querySelector("[data-testid=follow-ups]")).toBeNull();
});

it("follow-ups: two clicks in the same tick still send exactly once", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard block={followUps} />
    </ReplyCardSendProvider>,
  );
  const [first, second] = [
    ...host.querySelectorAll("[data-testid=follow-ups] button"),
  ] as HTMLButtonElement[];
  act(() => {
    first?.click();
    second?.click();
    first?.click();
  });
  expect(send).toHaveBeenCalledExactlyOnceWith("Compare with last year");
});

it("follow-ups: hidden control characters never reach the chip or the sent text", () => {
  const send = vi.fn();
  const host = mount(
    <ReplyCardSendProvider send={send}>
      <ReplyCard
        block={block({
          card: "follow_ups",
          data: { suggestions: ["Show\u200b it\u202e by region\ufeff"] },
        })}
      />
    </ReplyCardSendProvider>,
  );
  const chip = host.querySelector("[data-testid=follow-ups] button") as HTMLButtonElement;
  expect(chip.textContent).toBe("Show it by region");
  act(() => chip.click());
  expect(send).toHaveBeenCalledWith("Show it by region");
});

/** A capability's tiles, as the Shell would register them: a button per option. */
function Tiles({ options, chosen, locked, onPick }: AskPreviewProps) {
  return (
    <div data-testid="tiles">
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          data-preview-id={option.previewId}
          disabled={locked}
          aria-pressed={chosen === option.label}
          onClick={() => onPick(option.label)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

const themeAsk = block({
  card: "ask",
  data: {
    question: "Which look?",
    options: [
      ["nord", "Nord"],
      ["bauhaus", "Bauhaus"],
      ["minimal-white", "Minimal White"],
    ].map(([id, label], i) => ({
      id: `opt-${i + 1}`,
      label,
      preview: { kind: "deck-theme", id },
    })),
  },
});

it("previewed options: the registered tiles draw them, one pick sends once and locks", () => {
  const send = vi.fn();
  const host = mount(
    <AskPreviewsProvider value={{ "deck-theme": Tiles }}>
      <ReplyCardSendProvider send={send}>
        <ReplyCard block={themeAsk} />
      </ReplyCardSendProvider>
    </AskPreviewsProvider>,
  );
  const tiles = [...host.querySelectorAll<HTMLButtonElement>('[data-testid="tiles"] button')];
  expect(tiles.map((tile) => tile.dataset.previewId)).toEqual(["nord", "bauhaus", "minimal-white"]);
  act(() => {
    tiles[1]?.click();
    tiles[0]?.click();
  });
  act(() => tiles[1]?.click());
  expect(send).toHaveBeenCalledExactlyOnceWith("Bauhaus");
  expect(tiles[1]?.getAttribute("aria-pressed")).toBe("true");
  expect(tiles[0]?.disabled).toBe(true);
});

it("previewed options: an answered card is locked on the pick", () => {
  const send = vi.fn();
  const host = mount(
    <AskPreviewsProvider value={{ "deck-theme": Tiles }}>
      <ReplyCardSendProvider send={send}>
        <ReplyCard block={themeAsk} answer="Nord" />
      </ReplyCardSendProvider>
    </AskPreviewsProvider>,
  );
  const tiles = [...host.querySelectorAll<HTMLButtonElement>('[data-testid="tiles"] button')];
  expect(tiles[0]?.getAttribute("aria-pressed")).toBe("true");
  act(() => tiles[2]?.click());
  expect(send).not.toHaveBeenCalled();
});

it("previewed options with no registered tiles keep the plain buttons", () => {
  const host = mount(<ReplyCard block={themeAsk} />);
  expect(host.querySelector('[data-testid="tiles"]')).toBeNull();
  expect(host.textContent).toContain("Minimal White");
});
