import { useLingui } from "@lingui/react/macro";
import type { ReactNode } from "react";
import { useMemo } from "react";
import { SuperchatOrb, useOrbHome } from "../../../components/ai/orb";
import { CalendarGlyph, SearchGlyph, SparkGlyph } from "../../../components/chrome/SuperchatGlyphs";
import { SuperchatTile, type TileTone } from "../../../components/chrome/SuperchatTile";
import { greetingLead } from "./greeting";

/**
 * The empty Conversation's start page, above the composer (the old design notes "First run"):
 * Superchat's orb, a time-of-day greeting and one muted line, sitting on the Ask field (Shell.tsx
 * centers the composer between this and `EmptyConversationSuggestions`).
 */
export function EmptyConversationLead({ personName }: { personName: string }) {
  const { t } = useLingui();
  // Stable for the life of this empty state; a running clock here would be
  // motion the person never asked for.
  const lead = useMemo(() => greetingLead(new Date(), personName), [personName]);
  const orbHere = useOrbHome("hero");
  return (
    <div
      data-testid="empty-conversation"
      className="flex flex-1 flex-col justify-end px-5 pt-10 pb-5 sm:px-8"
    >
      <div className="mx-auto flex w-full max-w-[640px] flex-col items-center text-center motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 motion-safe:duration-500">
        {orbHere ? <SuperchatOrb size={96} /> : <span aria-hidden="true" className="size-24" />}
        <h1 className="mt-5 text-[30px] leading-[1.15] font-semibold tracking-[-0.4px] text-balance text-foreground">
          {lead}
        </h1>
        <p className="mt-3.5 text-[15px] leading-[1.47] tracking-[-0.24px] text-pretty text-ink-2">
          {t`Ask me anything, or tell me what you're working toward.`}
        </p>
      </div>
    </div>
  );
}

type StartApp = { label: string; prompt: string; tone: TileTone; glyph: ReactNode };

/**
 * The start page's app row, under the Ask field: colored 48px tiles with a label under each,
 * each sending its starting prompt at once.
 */
export function EmptyConversationSuggestions({ onSend }: { onSend: (text: string) => void }) {
  const { t } = useLingui();
  const apps: StartApp[] = [
    { label: t`Plan week`, prompt: t`Plan my week`, tone: "red", glyph: <CalendarGlyph /> },
    {
      label: t`Research`,
      prompt: t`Research something for me`,
      tone: "blue",
      glyph: <SearchGlyph />,
    },
    { label: t`Explore`, prompt: t`What can you do?`, tone: "purple", glyph: <SparkGlyph /> },
  ];
  return (
    <div className="flex flex-1 flex-col px-5 pt-3 pb-10 sm:px-8">
      <fieldset
        aria-label={t`Start with`}
        className="m-0 mx-auto flex min-w-0 flex-wrap justify-center gap-[22px] border-0 p-0 motion-safe:animate-in motion-safe:fade-in motion-safe:duration-700"
      >
        {apps.map((app) => (
          <button
            key={app.prompt}
            type="button"
            onClick={() => onSend(app.prompt)}
            className="group/app flex w-[72px] flex-col items-center gap-[7px] rounded-xl pb-1 text-[12px] tracking-[-0.08px] text-ink-2 transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <SuperchatTile
              tone={app.tone}
              size={48}
              className="rounded-[32%] shadow-[0_4px_12px_rgb(0_0_0/0.12)] transition-transform duration-200 group-hover/app:-translate-y-0.5 motion-reduce:transition-none"
            >
              {app.glyph}
            </SuperchatTile>
            {app.label}
          </button>
        ))}
      </fieldset>
    </div>
  );
}
