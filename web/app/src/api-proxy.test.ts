import http from "node:http";
import type { AddressInfo } from "node:net";
import { createServer, type ViteDevServer } from "vite";
import { afterEach, describe, expect, it } from "vitest";
import { apiProxy } from "./api-proxy";

const cleanups: Array<() => Promise<void> | void> = [];
afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
});

/** An API that opens an event stream and never ends it on its own. */
async function streamingApi() {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: open\n\n");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  cleanups.push(() => {
    server.closeAllConnections();
    server.close();
  });
  return { server, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}` };
}

async function devServer(target: string, proxied: boolean): Promise<string> {
  const vite: ViteDevServer = await createServer({
    configFile: false,
    logLevel: "silent",
    server: {
      host: "127.0.0.1",
      port: 0,
      proxy: { "/rpc": proxied ? apiProxy(target) : { target, changeOrigin: true } },
    },
  });
  await vite.listen();
  cleanups.push(() => vite.close());
  const address = vite.httpServer?.address() as AddressInfo;
  return `http://127.0.0.1:${address.port}`;
}

/** Opens a stream through the dev server, kills the API, and reports what the page sees. */
async function afterApiDies(proxied: boolean): Promise<"failed" | "ended" | "still open"> {
  const api = await streamingApi();
  const web = await devServer(api.url, proxied);
  const response = await fetch(`${web}/rpc/threads/subscribe`);
  const reader = response.body!.getReader();
  await reader.read();
  api.server.closeAllConnections();
  const outcome = reader.read().then(
    (chunk) => (chunk.done ? ("ended" as const) : ("still open" as const)),
    () => "failed" as const,
  );
  const still = new Promise<"still open">((resolve) =>
    setTimeout(() => resolve("still open"), 1500),
  );
  const result = await Promise.race([outcome, still]);
  await reader.cancel().catch(() => undefined);
  return result;
}

describe("apiProxy", () => {
  it("fails the page's stream when the API dies mid-stream, so it reconnects", async () => {
    expect(await afterApiDies(true)).toBe("failed");
  });

  it("guards a real gap: Vite's plain proxy leaves that stream open forever", async () => {
    expect(await afterApiDies(false)).toBe("still open");
  });
});
