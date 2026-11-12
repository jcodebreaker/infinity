import { useLingui } from "@lingui/react/macro";
import type { AutomationSchedule, Live, LiveTell } from "@superchat/contracts";
import {
  Button,
  Checkbox,
  Input,
  Label,
  NativeSelect,
  NativeSelectOption,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Textarea,
} from "@superchat/ui-web";
import { ChevronDown, Plus, X } from "lucide-react";
import { useState } from "react";
import { WEEKDAYS } from "../automations";
import type { LiveSettingsInput, useLive } from "./useLive";

const NOTE_MAX = 1000;
const DEFAULT_SCHEDULE: AutomationSchedule = { frequency: "weekly", time: "08:00", weekday: 0 };

type LiveApi = ReturnType<typeof useLive>;

/** "Keep updated" (or "Updates weekly ▾" once on) and the popover with the page's live settings. */
export function LiveSettings({ api, live }: { api: LiveApi; live: Live | null }) {
  const { t } = useLingui();
  const [open, setOpen] = useState(false);
  const frequency = live?.schedule?.frequency;
  const label = !live
    ? t`Keep updated`
    : live.onChange
      ? t`Updates when file changes`
      : frequency === "daily"
        ? t`Updates daily`
        : frequency === "weekly"
          ? t`Updates weekly`
          : frequency === "monthly"
            ? t`Updates monthly`
            : t`Updates on a schedule`;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={<Button variant={live ? "outline" : "secondary"} size="sm" />}
        data-testid="live-settings"
      >
        {label}
        {live ? <ChevronDown size={12} aria-hidden="true" /> : null}
      </PopoverTrigger>
      <PopoverContent align="end" className="max-h-[80vh] w-80 gap-3 overflow-y-auto p-3">
        {/* Mounted while open, so every opening starts from what is saved. */}
        <SettingsForm api={api} live={live} onClose={() => setOpen(false)} />
      </PopoverContent>
    </Popover>
  );
}

function SettingsForm({
  api,
  live,
  onClose,
}: {
  api: LiveApi;
  live: Live | null;
  onClose: () => void;
}) {
  const { t } = useLingui();
  const [schedule, setSchedule] = useState<AutomationSchedule>(live?.schedule ?? DEFAULT_SCHEDULE);
  const [sources, setSources] = useState<string[]>(live?.sources ?? []);
  const [tell, setTell] = useState<LiveTell>(live?.tell ?? "notable");
  const [onChange, setOnChange] = useState(live?.onChange ?? true);
  const [note, setNote] = useState(live?.note ?? "");
  const [fresh, setFresh] = useState(false);
  const [busy, setBusy] = useState(false);

  const done = async (work: () => Promise<unknown>) => {
    setBusy(true);
    const result = await work();
    setBusy(false);
    if (result) onClose();
  };
  const startFresh = async () => {
    setBusy(true);
    setFresh((await api.reset()) === true);
    setBusy(false);
  };
  const input = (): LiveSettingsInput => ({
    schedule,
    sources: sources.map((source) => source.trim()).filter(Boolean),
    tell,
    onChange,
    note: note.trim(),
  });
  const setFrequency = (frequency: AutomationSchedule["frequency"]) =>
    setSchedule({
      time: schedule.time,
      frequency,
      ...(frequency === "weekly" ? { weekday: schedule.weekday ?? 0 } : {}),
      ...(frequency === "monthly" ? { monthDay: schedule.monthDay ?? 1 } : {}),
    });
  const editSource = (index: number, value: string) =>
    setSources(sources.map((source, at) => (at === index ? value : source)));

  const field = "flex flex-col gap-1.5";
  const caption = "text-[12px] text-muted-foreground";
  return (
    <div className="flex flex-col gap-3" data-testid="live-form">
      <div className={field}>
        <span className={caption}>{t`Update when`}</span>
        <Label className="gap-2 text-[13px] font-normal">
          <Checkbox
            checked={onChange}
            onCheckedChange={(checked) => setOnChange(checked === true)}
          />
          {t`A file below changes`}
        </Label>
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[13px]">{t`And every`}</span>
          <NativeSelect
            className="shrink-0"
            aria-label={t`How often`}
            value={schedule.frequency}
            onChange={(event) =>
              setFrequency(event.target.value as AutomationSchedule["frequency"])
            }
          >
            <NativeSelectOption value="daily">{t`Day`}</NativeSelectOption>
            <NativeSelectOption value="weekly">{t`Week`}</NativeSelectOption>
            <NativeSelectOption value="monthly">{t`Month`}</NativeSelectOption>
          </NativeSelect>
        </div>
        <div className="flex gap-2">
          {schedule.frequency === "weekly" ? (
            <NativeSelect
              className="shrink-0"
              aria-label={t`Day of the week`}
              value={schedule.weekday ?? 0}
              onChange={(event) =>
                setSchedule({ ...schedule, weekday: Number(event.target.value) })
              }
            >
              {WEEKDAYS.map((day, index) => (
                <NativeSelectOption key={day} value={index}>
                  {day}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : null}
          {schedule.frequency === "monthly" ? (
            <NativeSelect
              className="shrink-0"
              aria-label={t`Day of the month`}
              value={schedule.monthDay ?? 1}
              onChange={(event) =>
                setSchedule({ ...schedule, monthDay: Number(event.target.value) })
              }
            >
              {Array.from({ length: 28 }, (_, index) => index + 1).map((day) => (
                <NativeSelectOption key={day} value={day}>
                  {day}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : null}
          <Input
            className="min-w-0 flex-1"
            type="time"
            aria-label={t`Time`}
            value={schedule.time}
            onChange={(event) => setSchedule({ ...schedule, time: event.target.value })}
          />
        </div>
      </div>

      <div className={field}>
        <span className={caption}>{t`Files it reads`}</span>
        {sources.map((source, index) => (
          <div key={index} className="flex items-center gap-1">
            <Input
              aria-label={t`File`}
              placeholder={t`File on your machine`}
              value={source}
              onChange={(event) => editSource(index, event.target.value)}
            />
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={t`Remove file`}
              onClick={() => setSources(sources.filter((_, at) => at !== index))}
            >
              <X />
            </Button>
          </div>
        ))}
        <Button
          variant="ghost"
          size="sm"
          className="self-start"
          onClick={() => setSources([...sources, ""])}
        >
          <Plus size={13} />
          {t`Add a file`}
        </Button>
      </div>

      <div className={field}>
        <span className={caption}>{t`What to update`}</span>
        <Textarea
          aria-label={t`What to update`}
          placeholder={t`e.g. only the prices; leave the title as it is`}
          maxLength={NOTE_MAX}
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </div>

      <div className={field}>
        <span className={caption}>{t`Tell me in chat`}</span>
        <NativeSelect
          aria-label={t`Tell me in chat`}
          value={tell}
          onChange={(event) => setTell(event.target.value as LiveTell)}
        >
          <NativeSelectOption value="notable">{t`When something important changes`}</NativeSelectOption>
          <NativeSelectOption value="every">{t`After every update`}</NativeSelectOption>
          <NativeSelectOption value="never">{t`Never`}</NativeSelectOption>
        </NativeSelect>
      </div>

      {api.error ? <p className="text-[13px] text-destructive">{api.error}</p> : null}
      {live ? (
        <div className="-mx-2 flex flex-wrap items-center gap-1">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void done(api.run)}>
            {t`Update now`}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy || fresh}
            onClick={() => void startFresh()}
          >
            {t`Start fresh`}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={busy}
            onClick={() => void done(api.stop)}
          >
            {t`Stop updating`}
          </Button>
        </div>
      ) : null}
      {fresh ? (
        <p data-testid="live-fresh" className="text-[12px] text-muted-foreground">
          {t`Starts fresh at the next update`}
        </p>
      ) : null}
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" disabled={busy} onClick={onClose}>
          {t`Cancel`}
        </Button>
        <Button size="sm" disabled={busy} onClick={() => void done(() => api.save(input()))}>
          {live ? t`Save` : t`Keep updated`}
        </Button>
      </div>
    </div>
  );
}
