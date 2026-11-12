import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { ArtifactVersion, AutomationRun, LivePage } from "@superchat/contracts";
import { Check, ChevronRight, X } from "lucide-react";
import { useEffect, useState } from "react";
import { QUIET_BUTTON } from "../../components/ui";
import { rpc } from "../../lib/rpc";
import {
  liveStatus,
  liveTrigger,
  plainCode,
  versionsByRun,
  whenSeconds,
  whenShort,
} from "./liveRuns";

/** The runs a row expands to. */
const RUN_LIMIT = 20;

function useRunHistory(page: LivePage, botId: string, enabled: boolean) {
  const [state, setState] = useState<{
    runs: AutomationRun[];
    versions: ArtifactVersion[];
  } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setFailed(false);
    Promise.all([
      rpc.automations.runs({ botId, id: page.taskId, limit: RUN_LIMIT }),
      rpc.artifacts.listVersions({ familyId: page.artifactId }).catch(() => []),
    ])
      .then(([runs, versions]) => !cancelled && setState({ runs, versions }))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [enabled, botId, page.taskId, page.artifactId, page.checkedAt]);
  return { state, failed };
}

function RunLine({
  run,
  version,
  newestReason,
  onOpenRun,
  onOpenVersion,
}: {
  run: AutomationRun;
  version: ArtifactVersion | undefined;
  /** The page's own reason for its latest failure, when this is that run. */
  newestReason: string | null;
  onOpenRun: (chatId: string) => void;
  onOpenVersion: (versionId: string) => void;
}) {
  const { t, i18n } = useLingui();
  const bad = run.status === "failed";
  const result =
    run.status === "running"
      ? t`Running…`
      : run.status === "scheduled"
        ? t`Waiting to start`
        : run.status === "skipped"
          ? t`Skipped`
          : bad
            ? t`Failed · ${newestReason ?? plainCode(run.errorCode) ?? t`it did not finish`}`
            : version
              ? (version.editSummary?.trim() ?? t`Updated`)
              : t`Checked · nothing new`;
  return (
    <li data-testid="live-run" className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
      <time className="w-24 shrink-0 text-[12.5px] text-ink-2">
        {whenShort(run.firedAt ?? run.scheduledAt, i18n.locale)}
      </time>
      <span
        className={`min-w-0 flex-1 text-[13px] ${bad ? "text-destructive" : "text-foreground"}`}
      >
        {result}
      </span>
      {version ? (
        <button
          type="button"
          className="text-[12.5px] text-foreground underline-offset-2 hover:underline"
          onClick={() => onOpenVersion(version.id)}
        >
          {t`Version ${version.version}`}
        </button>
      ) : null}
      {run.chatId ? (
        <button
          type="button"
          className="text-[12.5px] text-ink-2 underline-offset-2 hover:text-foreground hover:underline"
          onClick={() => run.chatId && onOpenRun(run.chatId)}
        >
          {t`Open run`}
        </button>
      ) : null}
    </li>
  );
}

function RunHistory({
  page,
  botId,
  onOpenRun,
  onOpenVersion,
}: {
  page: LivePage;
  botId: string;
  onOpenRun: (chatId: string) => void;
  onOpenVersion: (versionId: string) => void;
}) {
  const { t } = useLingui();
  const { state, failed } = useRunHistory(page, botId, true);
  if (failed) return <p className="text-[13px] text-destructive">{t`Could not load the runs.`}</p>;
  if (!state) return <p className="text-[13px] text-ink-2">{t`Loading…`}</p>;
  if (state.runs.length === 0) {
    return <p className="text-[13px] text-ink-2">{t`No runs yet.`}</p>;
  }
  const made = versionsByRun(state.runs, state.versions);
  return (
    <ul data-testid="live-runs" className="flex flex-col divide-y divide-glass-line">
      {state.runs.map((run, index) => (
        <RunLine
          key={run.id}
          run={run}
          version={made.get(run.id)}
          newestReason={index === 0 ? (page.lastFailure?.reason ?? null) : null}
          onOpenRun={onOpenRun}
          onOpenVersion={onOpenVersion}
        />
      ))}
    </ul>
  );
}

/**
 * A live page in the Automations list: "Keeps <page> updated", what starts it, how its last run
 * went, and its last 20 runs. A run opens in the Activity run page, a version it made in the page.
 */
export function LivePageRow({
  page,
  botId,
  onOpenPage,
  onOpenRun,
  onResume,
}: {
  page: LivePage;
  botId: string;
  onOpenPage: (artifactId: string) => void;
  onOpenRun: (chatId: string) => void;
  onResume: () => void;
}) {
  const { t, i18n } = useLingui();
  const [open, setOpen] = useState(false);
  const title = page.title?.trim() || page.name;
  const status = liveStatus(page);
  const trigger = liveTrigger(page, { when: (files) => t`When ${files} changes` });

  return (
    <li
      data-testid="live-page-row"
      className="flex flex-col gap-2 rounded-2xl border border-glass-line bg-foreground/[0.04] p-4"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? t`Hide the runs` : t`Show the runs`}
          onClick={() => setOpen((on) => !on)}
          className="grid size-7 shrink-0 place-items-center rounded-lg text-ink-2 hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
        >
          <ChevronRight
            size={16}
            aria-hidden="true"
            className={open ? "rotate-90 transition-transform" : "transition-transform"}
          />
        </button>
        <div className="flex min-w-[200px] flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              data-testid="live-page-title"
              onClick={() => onOpenPage(page.artifactId)}
              className="truncate text-start text-[15px] font-medium text-foreground underline-offset-2 hover:underline"
              dir="auto"
            >
              {t`Keeps ${title} updated`}
            </button>
            <span className="shrink-0 rounded-full bg-selection px-2 py-0.5 text-[11px] text-ink-2">
              {/\.(csv|xlsx)$/i.test(page.name) ? t`Live sheet` : t`Live page`}
            </span>
          </span>
          <span className="text-[13px] text-ink-2">{trigger}</span>
        </div>
        <div
          data-testid="live-status"
          className={`flex min-w-[160px] items-center gap-1.5 text-[13px] ${
            status.kind === "failed" || (status.kind === "paused" && status.failures > 0)
              ? "text-destructive"
              : "text-ink-2"
          }`}
        >
          {status.kind === "failed" ? (
            <X size={14} aria-hidden="true" />
          ) : status.kind === "changed" || status.kind === "checked" ? (
            <Check size={14} aria-hidden="true" />
          ) : null}
          <span>
            {status.kind === "paused"
              ? status.failures > 0
                ? t`Paused after ${status.failures} failed runs`
                : t`Paused`
              : status.kind === "failed"
                ? t`Failed · ${status.reason}`
                : status.kind === "changed"
                  ? `${plural(status.changes, { one: "# change", other: "# changes" })} · ${whenSeconds(status.at, i18n.locale)}`
                  : status.kind === "checked"
                    ? t`Checked · nothing new`
                    : t`Not run yet`}
          </span>
        </div>
        {status.kind === "paused" ? (
          <button type="button" className={QUIET_BUTTON} onClick={onResume}>
            {t`Resume`}
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="ms-10">
          <RunHistory page={page} botId={botId} onOpenRun={onOpenRun} onOpenVersion={onOpenPage} />
        </div>
      ) : null}
    </li>
  );
}
