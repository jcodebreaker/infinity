/** A dashboard page: one self-contained HTML file of KPIs and Chart.js charts, built in the
 * host by `superchat-dashboard` and named `*.dashboard.html`. */
export const DASHBOARD_PAGE_NAME_SUFFIX = ".dashboard.html";

/** True when an artifact file name marks a dashboard page (`*.dashboard.html`, any case). */
export function isDashboardPageName(name: string): boolean {
  return (
    name.length > DASHBOARD_PAGE_NAME_SUFFIX.length &&
    name.toLowerCase().endsWith(DASHBOARD_PAGE_NAME_SUFFIX)
  );
}
