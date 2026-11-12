import { useLingui } from "@lingui/react/macro";
import { useCallback, useEffect, useState } from "react";
import { MemorySection } from "../../components/ui/MemorySection";

export type DailyNote = {
  date: string;
  sections: Record<string, string>;
  editedByPerson: boolean;
  finalized: boolean;
  updatedAt: number;
};

/** The Days section's wire: `rpc.dailyNotes.list` / `rpc.dailyNotes.save`. */
export type DaysWire = {
  list: (input: { botId: string }) => Promise<{ notes: DailyNote[] }>;
  save: (input: {
    botId: string;
    date: string;
    sections: Record<string, string>;
  }) => Promise<DailyNote>;
};

/** The engine's section keys, in reading order. */
export const DAY_SECTION_KEYS = [
  "talked_about",
  "decisions",
  "promised",
  "open_loops",
  "reflection",
] as const;

function Day({
  botId,
  note,
  wire,
  onSaved,
}: {
  botId: string;
  note: DailyNote;
  wire: DaysWire;
  onSaved: (note: DailyNote) => void;
}) {
  const { t } = useLingui();
  const titles: Record<string, string> = {
    talked_about: t`What we talked about`,
    decisions: t`Decisions`,
    promised: t`Promised`,
    open_loops: t`Open loops`,
    reflection: t`Reflection`,
  };
  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setFailed(false);
    try {
      onSaved(await wire.save({ botId, date: note.date, sections: draft }));
      setDraft(null);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  const filled = DAY_SECTION_KEYS.filter((key) => (note.sections[key] ?? "").trim() !== "");
  return (
    <li
      className="group/day superchat-row superchat-row-plain px-3 py-2.5 transition-colors hover:bg-selection focus-within:bg-selection"
      data-testid="day-note"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-[14px] leading-5 font-medium text-foreground">{note.date}</h4>
        {draft ? null : (
          <button
            type="button"
            className="text-[13px] text-link opacity-0 outline-none transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover/day:opacity-100 [@media(hover:none)]:opacity-100"
            onClick={() => setDraft({ ...note.sections })}
          >
            {t`Edit`}
          </button>
        )}
      </div>
      {draft ? (
        <div className="mt-2 flex flex-col gap-2">
          {DAY_SECTION_KEYS.map((key) => (
            <label key={key} className="flex flex-col gap-1">
              <span className="text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
                {titles[key]}
              </span>
              <textarea
                className="min-h-16 rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-[14px] leading-5 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                dir="auto"
                value={draft[key] ?? ""}
                onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
              />
            </label>
          ))}
          {failed ? (
            <p className="text-[12px] text-destructive">{t`Could not save this day`}</p>
          ) : null}
          <div className="flex gap-3 text-[13px]">
            <button type="button" disabled={saving} onClick={() => void save()}>
              {t`Save`}
            </button>
            <button
              type="button"
              className="text-muted-foreground"
              disabled={saving}
              onClick={() => setDraft(null)}
            >
              {t`Cancel`}
            </button>
          </div>
        </div>
      ) : filled.length === 0 ? (
        <p className="text-[12px] leading-4 text-muted-foreground">{t`Nothing noted yet.`}</p>
      ) : (
        <div className="mt-0.5 flex flex-col gap-1.5">
          {filled.map((key) => (
            <div key={key}>
              <p className="text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
                {titles[key]}
              </p>
              <p className="whitespace-pre-line text-[14px] leading-5" dir="auto">
                {note.sections[key]}
              </p>
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

/** The Memory tab's "Days" section: recent daily notes, readable and editable in place. A
 * failed or unavailable load renders nothing (the Memory Profile above stays the tab's job). */
export function DaysSection({ botId, wire }: { botId: string; wire: DaysWire }) {
  const { t } = useLingui();
  const [notes, setNotes] = useState<DailyNote[]>([]);

  useEffect(() => {
    let live = true;
    wire
      .list({ botId })
      .then((result) => {
        if (live) setNotes(result.notes);
      })
      .catch(() => {
        if (live) setNotes([]);
      });
    return () => {
      live = false;
    };
  }, [botId, wire]);

  const replace = useCallback(
    (saved: DailyNote) =>
      setNotes((current) => current.map((n) => (n.date === saved.date ? saved : n))),
    [],
  );

  if (notes.length === 0) return null;
  return (
    <div data-testid="memory-days">
      <MemorySection id="days" title={t`Days`} count={notes.length}>
        <ul className="superchat-group flex flex-col">
          {notes.map((note) => (
            <Day key={note.date} botId={botId} note={note} wire={wire} onSaved={replace} />
          ))}
        </ul>
      </MemorySection>
    </div>
  );
}
