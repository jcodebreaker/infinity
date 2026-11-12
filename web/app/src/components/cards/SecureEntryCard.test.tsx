// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children?: unknown }) => <>{children}</>,
  };
});
vi.mock("@superchat/ui-web", () => ({
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
  Card: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  CardAction: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  CardContent: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  CardHeader: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  CardTitle: ({ children }: { children?: unknown }) => <h3>{children as never}</h3>,
  Button: (props: object) => <button type="button" {...props} />,
  Input: (props: object) => <input {...props} />,
}));
const api = vi.hoisted(() => ({
  request: vi.fn(async () => ({ status: "pending" })),
  save: vi.fn(async () => ({ id: "s1" })),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { vault: api } }));

import { ReplyCardBotProvider } from "./context";
import { SecureEntryCard } from "./SecureEntryCard";

const mounted: HTMLElement[] = [];
afterEach(() => {
  for (const node of mounted.splice(0)) node.remove();
  vi.clearAllMocks();
});

function type(input: HTMLInputElement, value: string) {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  set?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

it("posts the password to the vault API only, empties the field and then says Saved", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  mounted.push(host);
  await act(async () => {
    createRoot(host).render(
      <ReplyCardBotProvider botId="bot1">
        <SecureEntryCard
          data={{ requestId: "r1", name: "acme", site: "https://www.acme.test", reason: "sign in" }}
        />
      </ReplyCardBotProvider>,
    );
  });
  const [user, pass] = Array.from(host.querySelectorAll("input"));
  expect(pass?.type).toBe("password");
  await act(async () => {
    if (user && pass) {
      type(user, "ann");
      type(pass, "hunter2");
    }
  });
  await act(async () => {
    host
      .querySelector("form")
      ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  expect(api.save).toHaveBeenCalledWith({
    botId: "bot1",
    requestId: "r1",
    name: "acme",
    site: "https://www.acme.test",
    username: "ann",
    password: "hunter2",
  });
  expect(host.querySelector("[data-testid=secure-entry-saved]")).not.toBeNull();
  expect(host.textContent).not.toContain("hunter2");
});

it("opens as Saved when the request was already answered", async () => {
  api.request.mockResolvedValueOnce({ status: "saved" });
  const host = document.createElement("div");
  document.body.append(host);
  mounted.push(host);
  await act(async () => {
    createRoot(host).render(
      <ReplyCardBotProvider botId="bot1">
        <SecureEntryCard data={{ requestId: "r1", name: "acme", site: "https://acme.test" }} />
      </ReplyCardBotProvider>,
    );
  });
  expect(host.querySelector("[data-testid=secure-entry-saved]")).not.toBeNull();
  expect(host.querySelector("input")).toBeNull();
});
