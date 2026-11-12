/** Whether this path opens the app, so its first data can be requested before the router mounts. */
export function isBootstrapRoute(pathname: string) {
  return pathname === "/app" || pathname.startsWith("/app/");
}
