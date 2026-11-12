import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { ReactNode } from "react";

i18n.loadAndActivate({ locale: "en", messages: {} });

type Client = typeof import("react-dom/client");

/**
 * Wraps react-dom/client so every `createRoot(...).render(node)` mounts inside an
 * I18nProvider. Use from a test file as:
 *   vi.mock("react-dom/client", async (orig) =>
 *     (await import("<rel>/test/i18n")).withI18nRoot(await orig<Client>()));
 */
export function withI18nRoot(client: Client): Client {
  const wrap = (node: ReactNode) => <I18nProvider i18n={i18n}>{node}</I18nProvider>;
  return {
    ...client,
    createRoot: (...args: Parameters<Client["createRoot"]>) => {
      const root = client.createRoot(...args);
      return {
        ...root,
        render: (node: ReactNode) => root.render(wrap(node)),
        unmount: () => root.unmount(),
      };
    },
  };
}
