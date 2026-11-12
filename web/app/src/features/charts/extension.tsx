import { t } from "@lingui/core/macro";
import { isDashboardPageName } from "@superchat/contracts";
import type { ArtifactExtension } from "../artifacts";
import { DashboardPageResult } from "./DashboardPage";
import { isLegacyChartName, LegacyChartNote } from "./LegacyChart";

/** A dashboard page (`*.dashboard.html`) shows live at full width in the chat and opens in a wide
 * panel. A chart file from the retired chart formats shows as a one-line note in the chat. */
export const chartsExtension: ArtifactExtension = {
  usePanel: ({ artifact }) => ({ wide: !!artifact && isDashboardPageName(artifact.name) }),
  card: (artifact) => (isDashboardPageName(artifact.name) ? { meta: t`Dashboard` } : null),
  result: ({ artifactId, name, version }) =>
    isDashboardPageName(name) ? (
      <DashboardPageResult artifactId={artifactId} name={name} version={version} />
    ) : isLegacyChartName(name) ? (
      <LegacyChartNote />
    ) : null,
};
