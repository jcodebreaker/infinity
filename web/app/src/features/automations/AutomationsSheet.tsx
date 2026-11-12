import { Trans, useLingui } from "@lingui/react/macro";
import type { Automation, AutomationSchedule, SystemJob } from "@superchat/contracts";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Input,
  NativeSelect,
  NativeSelectOption,
  Switch,
  Textarea,
} from "@superchat/ui-web";
import { Ellipsis, Play, Plus } from "lucide-react";
import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { DockSheet, DockSheetHeader } from "../../components/chrome/DockSheet";
import { EmptyState, PRIMARY_BUTTON, QUIET_BUTTON } from "../../components/ui";
import { CardSkeletonList } from "../../components/ui/CardSkeleton";
import { formatRelativeTime } from "../../lib/relative-time";
import { rpc } from "../../lib/rpc";
import { ActivityRunById } from "../activity";
import { LivePageRow } from "./LivePageRow";
import { describeSchedule, WEEKDAYS } from "./schedule";
import { type AutomationsWire, useAutomations } from "./useAutomations";
import { useLivePageRows } from "./useLivePageRows";

const FIELD =
  "min-h-11 w-full rounded-xl border border-glass-line bg-black/20 px-3.5 text-[14px] text-foreground placeholder:text-ink-2";
/** The shadcn select at the same height and fill as the inputs. */
const SELECT =
  "w-full [&_select]:h-11 [&_select]:rounded-xl [&_select]:border-glass-line [&_select]:bg-black/20 [&_select]:ps-3.5 [&_select]:text-[14px] [&_select]:text-foreground";
const LABEL = "text-[13px] font-medium text-ink-2";

function nextRun(automation: Automation, locale: string | undefined): string {
  if (!automation.enabled) return "Paused";
  if (!automation.nextRunAt) return "Not scheduled";
  const date = new Date(automation.nextRunAt);
  if (Number.isNaN(date.getTime())) return "Not scheduled";
  return date.toLocaleString(locale || "en", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function lastRun(automation: Automation): string {
  if (automation.lastRunStatus === "running") return "Running…";
  if (!automation.lastRunAt) return "Never run";
  const when = formatRelativeTime(automation.lastRunAt);
  return automation.lastRunStatus === "failed" ? `Failed ${when}` : `Ran ${when}`;
}

function systemNext(job: SystemJob, locale: string | undefined): string {
  if (!job.enabled) return "Off";
  if (!job.nextRunAt) return "Not known yet";
  const date = new Date(job.nextRunAt);
  if (Number.isNaN(date.getTime())) return "Not known yet";
  return date.toLocaleString(locale || "en", {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** A built-in background job: read-only, no run or delete. */
function SystemJobRow({ job }: { job: SystemJob }) {
  const { t, i18n } = useLingui();
  return (
    <li
      data-testid="system-job-row"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-glass-line bg-foreground/[0.02] p-4"
    >
      <div className="flex min-w-[200px] flex-1 flex-col gap-0.5">
        <span className="truncate text-[15px] font-medium text-foreground">{job.name}</span>
        <span className="text-[13px] text-ink-2">{job.description}</span>
        <span className="text-[13px] text-ink-2">{job.schedule}</span>
      </div>
      <div className="flex min-w-[160px] flex-col gap-0.5 text-[13px] text-ink-2">
        <span>
          {t`Next`}: {systemNext(job, i18n.locale)}
        </span>
        <span>
          {job.lastRunAt ? `${t`Ran`} ${formatRelativeTime(job.lastRunAt)}` : t`Not run yet`}
        </span>
        {job.lastOutcome ? (
          <span
            data-testid="system-job-outcome"
            className={job.lastFailed ? "text-destructive" : undefined}
          >
            {job.lastOutcome}
          </span>
        ) : null}
      </div>
    </li>
  );
}

function SystemSection({ jobs }: { jobs: SystemJob[] | null }) {
  const { t } = useLingui();
  if (!jobs?.length) return null;
  return (
    <section className="mt-3 flex flex-col gap-3" data-testid="system-jobs">
      <div className="flex flex-col gap-0.5">
        <h3 className="text-[15px] font-medium text-foreground">{t`System`}</h3>
        <p className="text-[13px] text-ink-2">{t`What I do on my own, in the background.`}</p>
      </div>
      <ul className="flex flex-col gap-3">
        {jobs.map((job) => (
          <SystemJobRow key={job.id} job={job} />
        ))}
      </ul>
    </section>
  );
}

type ModelChoice = { id: string; label: string };

/** The models this person can run, from the same catalog as the Model button; none hides the field. */
function useModelChoices(botId: string) {
  const [choices, setChoices] = useState<{ models: ModelChoice[]; defaultLabel: string | null }>({
    models: [],
    defaultLabel: null,
  });
  useEffect(() => {
    let cancelled = false;
    rpc.engineModels
      .sessionModel({ botId })
      .then(({ catalog }) => {
        if (cancelled || catalog.status !== "ready") return;
        setChoices({
          models: catalog.models.map((model) => ({ id: model.id, label: model.label })),
          // Without a pick an automation runs on the person's Light model, like any background
          // Helper.
          defaultLabel:
            catalog.models.find((model) => model.id === catalog.lightModel)?.label ?? null,
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [botId]);
  return choices;
}

function NewAutomationForm({
  botId,
  onSubmit,
  onCancel,
}: {
  botId: string;
  onSubmit: (input: {
    name: string;
    prompt: string;
    schedule: AutomationSchedule;
    model?: string;
  }) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLingui();
  const choices = useModelChoices(botId);
  const [model, setModel] = useState("");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [frequency, setFrequency] = useState<AutomationSchedule["frequency"]>("daily");
  const [weekday, setWeekday] = useState(0);
  const [monthDay, setMonthDay] = useState(1);
  const [time, setTime] = useState("09:00");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !name.trim() || !prompt.trim() || !time) return;
    setBusy(true);
    setError(null);
    const schedule: AutomationSchedule =
      frequency === "daily" || frequency === "weekdays"
        ? { frequency, time }
        : frequency === "weekly"
          ? { frequency, time, weekday }
          : { frequency, time, monthDay };
    try {
      await onSubmit({
        name: name.trim(),
        prompt: prompt.trim(),
        schedule,
        ...(model ? { model } : {}),
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t`Could not save this automation.`);
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      data-testid="automation-form"
      className="flex w-full flex-col gap-5"
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="auto-name" className={LABEL}>{t`Name`}</label>
        <Input
          id="auto-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t`Weekly summary`}
          maxLength={120}
          className={FIELD}
          autoFocus
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="auto-prompt" className={LABEL}>{t`What should superchat do?`}</label>
        <Textarea
          id="auto-prompt"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder={t`Summarize what changed in my files this week and tell me what needs attention.`}
          rows={5}
          maxLength={8000}
          className={`${FIELD} min-h-[120px] resize-none py-3 leading-relaxed`}
        />
      </div>
      {choices.models.length ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="auto-model" className={LABEL}>{t`Model`}</label>
          <NativeSelect
            id="auto-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            aria-label={t`Model`}
            className={SELECT}
          >
            <NativeSelectOption value="">
              {choices.defaultLabel ? t`Light · ${choices.defaultLabel}` : t`Light model`}
            </NativeSelectOption>
            {choices.models.map((choice) => (
              <NativeSelectOption key={choice.id} value={choice.id}>
                {choice.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      ) : null}
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] items-start gap-3">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="auto-freq" className={LABEL}>{t`Repeat`}</label>
          <NativeSelect
            id="auto-freq"
            value={frequency}
            onChange={(event) => setFrequency(event.target.value as typeof frequency)}
            aria-label={t`Repeat`}
            className={SELECT}
          >
            <NativeSelectOption value="daily">{t`Daily`}</NativeSelectOption>
            <NativeSelectOption value="weekdays">{t`Weekdays`}</NativeSelectOption>
            <NativeSelectOption value="weekly">{t`Weekly`}</NativeSelectOption>
            <NativeSelectOption value="monthly">{t`Monthly`}</NativeSelectOption>
          </NativeSelect>
        </div>
        {frequency === "weekly" ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor="auto-day" className={LABEL}>{t`On`}</label>
            <NativeSelect
              id="auto-day"
              value={weekday}
              onChange={(event) => setWeekday(Number(event.target.value))}
              aria-label={t`Day of the week`}
              className={SELECT}
            >
              {WEEKDAYS.map((day, index) => (
                <NativeSelectOption key={day} value={index}>
                  {day}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
        ) : null}
        {frequency === "monthly" ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor="auto-monthday" className={LABEL}>{t`On day`}</label>
            <NativeSelect
              id="auto-monthday"
              value={monthDay}
              onChange={(event) => setMonthDay(Number(event.target.value))}
              aria-label={t`Day of the month`}
              className={SELECT}
            >
              {Array.from({ length: 28 }, (_, index) => index + 1).map((day) => (
                <NativeSelectOption key={day} value={day}>
                  {day}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
        ) : null}
        <div className="flex flex-col gap-1.5">
          <label htmlFor="auto-time" className={LABEL}>{t`At`}</label>
          <Input
            id="auto-time"
            type="time"
            value={time}
            onChange={(event) => setTime(event.target.value)}
            className={FIELD}
            required
          />
        </div>
      </div>
      {error ? <p className="text-[13.5px] text-destructive">{error}</p> : null}
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          className={`${QUIET_BUTTON} h-11 px-4`}
          onClick={onCancel}
          disabled={busy}
        >
          <Trans>Cancel</Trans>
        </button>
        <button
          type="submit"
          className={`${PRIMARY_BUTTON} h-11 px-5`}
          disabled={busy || !name.trim() || !prompt.trim()}
        >
          {busy ? t`Saving…` : t`Create automation`}
        </button>
      </div>
    </form>
  );
}

function AutomationRow({
  automation,
  onToggle,
  onRun,
  onDelete,
}: {
  automation: Automation;
  onToggle: (on: boolean) => void;
  onRun: () => void;
  onDelete: () => void;
}) {
  const { t, i18n } = useLingui();
  return (
    <li
      data-testid="automation-row"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-glass-line bg-foreground/[0.04] p-4"
    >
      <div className="flex min-w-[200px] flex-1 flex-col gap-0.5">
        <span className="truncate text-[15px] font-medium text-foreground" dir="auto">
          {automation.name}
        </span>
        <span className="text-[13px] text-ink-2">{describeSchedule(automation.schedule)}</span>
      </div>
      <div className="flex min-w-[160px] flex-col gap-0.5 text-[13px] text-ink-2">
        <span>
          {t`Next`}: {nextRun(automation, i18n.locale)}
        </span>
        <span className={automation.lastRunStatus === "failed" ? "text-destructive" : undefined}>
          {lastRun(automation)}
        </span>
      </div>
      <button type="button" className={QUIET_BUTTON} onClick={onRun}>
        <Play size={14} aria-hidden="true" className="me-1.5" />
        <Trans>Run now</Trans>
      </button>
      <Switch
        checked={automation.enabled}
        onCheckedChange={onToggle}
        aria-label={t`${automation.name} is on`}
      />
      {automation.removable === false ? null : (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <button
                type="button"
                aria-label={t`More actions for ${automation.name}`}
                className="grid size-9 place-items-center rounded-lg text-ink-2 hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
              />
            }
          >
            <Ellipsis size={18} aria-hidden="true" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem variant="destructive" onClick={onDelete}>
              <Trans>Delete</Trans>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </li>
  );
}

/** Automations: the engine's scheduled tasks, as a list and a plain-choices form. */
export function AutomationsSheet({
  botId,
  open,
  onClose,
  wire,
  renderPage,
}: {
  botId: string;
  open: boolean;
  onClose: () => void;
  /** Dev previews pass their own; the app uses the live RPC. */
  wire?: AutomationsWire;
  /** Shows a live page (or one of its versions) in place of the list; the host brings the
   * artifact editor. Without it a page title is not a link. */
  renderPage?: (page: { artifactId: string; back: () => void; close: () => void }) => ReactNode;
}) {
  const { t } = useLingui();
  const { state, systemJobs, create, setEnabled, runNow, remove } = useAutomations(
    botId,
    open,
    wire,
  );
  const livePages = useLivePageRows(open);
  const [openPageId, setOpenPageId] = useState<string | null>(null);
  const [openRunChat, setOpenRunChat] = useState<string | null>(null);
  const closeRun = useCallback(() => setOpenRunChat(null), []);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const act = (action: () => Promise<void>) => {
    setActionError(null);
    action().catch((cause: unknown) =>
      setActionError(cause instanceof Error ? cause.message : t`That did not work.`),
    );
  };

  return (
    <DockSheet
      open={open}
      title={t`Automations`}
      // The form is short: the sheet shrinks to it instead of floating in a big empty panel.
      className={
        creating
          ? "h-auto w-[min(620px,calc(100%-1.5rem))] md:w-[min(620px,calc(100%-9rem))]"
          : undefined
      }
      bare={openPageId !== null}
      onEscape={() => {
        if (openPageId === null) return false;
        setOpenPageId(null);
        return true;
      }}
      onClose={() => {
        setCreating(false);
        setOpenPageId(null);
        onClose();
      }}
    >
      <div className={openPageId ? "hidden" : "contents"}>
        <DockSheetHeader
          title={creating ? t`New automation` : t`Automations`}
          subtitle={
            creating
              ? t`Pick a time and I'll do this for you.`
              : t`Things I do for you on a schedule.`
          }
        >
          {creating ? null : (
            <button type="button" className={PRIMARY_BUTTON} onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden="true" className="me-1.5" />
              <Trans>New automation</Trans>
            </button>
          )}
        </DockSheetHeader>
        <div className="sc-scroll flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
          {creating ? (
            <NewAutomationForm
              botId={botId}
              onCancel={() => setCreating(false)}
              onSubmit={async (input) => {
                await create(input);
                setCreating(false);
              }}
            />
          ) : state.status === "loading" ? (
            <CardSkeletonList count={3} />
          ) : state.status === "error" ? (
            <p className="py-10 text-[14px] text-destructive">{state.message}</p>
          ) : state.automations.length === 0 && !livePages.pages?.length ? (
            <EmptyState headline={t`No automations yet`}>
              {t`Create one and I'll run it on its own, every day, week or month.`}
            </EmptyState>
          ) : (
            <ul className="flex flex-col gap-3" data-testid="automation-list">
              {livePages.pages?.map((page) => (
                <LivePageRow
                  key={page.taskId}
                  page={page}
                  botId={botId}
                  onOpenPage={(artifactId) => renderPage && setOpenPageId(artifactId)}
                  onOpenRun={setOpenRunChat}
                  onResume={() => act(() => livePages.resume(page.artifactId))}
                />
              ))}
              {state.automations.map((automation) => (
                <AutomationRow
                  key={automation.id}
                  automation={automation}
                  onToggle={(on) => act(() => setEnabled(automation.id, on))}
                  onRun={() => act(() => runNow(automation.id))}
                  onDelete={() => act(() => remove(automation.id))}
                />
              ))}
            </ul>
          )}
          {creating ? null : <SystemSection jobs={systemJobs} />}
          {actionError ? <p className="text-[13.5px] text-destructive">{actionError}</p> : null}
        </div>
      </div>
      {open && openPageId && renderPage
        ? renderPage({
            artifactId: openPageId,
            back: () => setOpenPageId(null),
            close: () => {
              setOpenPageId(null);
              onClose();
            },
          })
        : null}
      {openRunChat ? (
        <ActivityRunById botId={botId} chatId={openRunChat} onClose={closeRun} />
      ) : null}
    </DockSheet>
  );
}
