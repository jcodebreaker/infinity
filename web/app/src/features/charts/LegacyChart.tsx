import { Trans } from "@lingui/react/macro";

/** Files the old `display_chart` / `display_dashboard` tools saved (`*.chart.json`,
 * `*.dashboard.json`); old transcripts still name them. */
export const isLegacyChartName = (name: string) => /.\.(chart|dashboard)\.json$/i.test(name);

/** What an old transcript shows where a chart from a retired chart format was. */
export function LegacyChartNote() {
  return (
    <p className="text-[13.5px] text-muted-foreground" data-legacy-chart>
      <Trans>This chart was made with an older version</Trans>
    </p>
  );
}
