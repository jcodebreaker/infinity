import type { IncomingMessage, ServerResponse } from "node:http";

/** The slice of Vite's proxy server this module listens on. */
interface ProxyEvents {
  on(
    event: "proxyRes",
    listener: (proxyRes: IncomingMessage, req: IncomingMessage, res: ServerResponse) => void,
  ): unknown;
}

/**
 * Vite's proxy keeps the browser's side of a response open when the API dies after the headers
 * were sent (an API restart in the middle of an RPC stream): the page never learns the stream is
 * gone, never reconnects, and the dead stream keeps one of the six connections a browser opens
 * per origin. A few restarts later every new request (a chart's document, a poll) waits forever.
 * Cutting the browser's response when the API's ends early surfaces the failure, so streams
 * reconnect and requests fail instead of hanging.
 */
export function endWithUpstream(proxy: ProxyEvents): void {
  proxy.on("proxyRes", (proxyRes, _req, res) => {
    proxyRes.on("close", () => {
      if (!proxyRes.complete && !res.writableEnded) res.destroy();
    });
  });
}

/** Proxy options for an API route: the target, and dead upstream streams cut through. */
export function apiProxy(target: string) {
  return { target, changeOrigin: true, configure: endWithUpstream };
}
