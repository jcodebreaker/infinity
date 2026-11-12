import { Trans } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import type { ReactNode, Ref } from "react";
import { SuperchatLogo } from "../../../components/SuperchatLogo";
import { ContourGround } from "./ContourGround";
import { SettingsButton } from "./Dock";
import { firstName } from "./greeting";
import { usePullPastEnd } from "./homeSlide";

/**
 * The home screen: the mark and wordmark top-left, the dock on the left, the greeting over the
 * composer in the middle with the person's topics under it, recent chats bottom-right and
 * Settings bottom-left, and "Continue the conversation" floating at the bottom centre. It is the
 * app's first screen and sits over the Conversation shell; continuing (the pill, or scrolling or
 * swiping on past its end) slides the Conversation in, whose Home button slides it back. Its
 * contour ground lifts into a peak under each topic card.
 */
export function Home({
  personName,
  dock,
  composer,
  topics,
  recent,
  onOpenSettings,
  continuePill,
  onContinue,
  ref,
  away = false,
}: {
  personName: string;
  dock: ReactNode;
  composer: ReactNode;
  /** "Pick up where you left off" under the composer (ADR 0035); absent while there are none. */
  topics?: ReactNode;
  recent: ReactNode;
  onOpenSettings: () => void;
  /** "Continue the conversation"; absent while the Conversation is empty. */
  continuePill?: ReactNode;
  /** Scrolling or swiping on past Home's end continues too (with the pill). */
  onContinue?: () => void;
  /** The layer the slide moves. */
  ref?: Ref<HTMLDivElement>;
  /** Sliding away: out of reach of the keyboard and the pointer. */
  away?: boolean;
}) {
  // Only a real first name: never a placeholder like "local" or a login id.
  const name = firstName(personName);
  const pull = usePullPastEnd(continuePill && onContinue ? onContinue : null);
  return (
    <div
      ref={ref}
      data-testid="home"
      inert={away || undefined}
      className="absolute inset-0 z-30 overflow-hidden bg-window text-foreground"
    >
      <div
        {...pull}
        className={cn(
          "absolute inset-0 flex flex-col gap-5 overflow-y-auto px-4 py-6 md:px-8 md:py-7",
          continuePill && "pb-24",
        )}
      >
        <ContourGround />
        <header className="relative flex items-center justify-between gap-3">
          <SuperchatLogo />
        </header>
        <div className="relative flex flex-1 flex-col gap-8 md:block">
          {/* The dock floats on the left so the greeting centres on the whole window. */}
          <div className="flex flex-none md:absolute md:inset-y-0 md:start-0 md:z-10 md:items-center">
            {dock}
          </div>
          <main className="flex min-h-[300px] min-w-0 flex-col items-center justify-center-safe gap-8 md:min-h-full md:px-28">
            <h1
              className="max-w-[680px] text-center text-[34px] leading-[1.12] font-medium tracking-[-0.03em] text-balance text-foreground/90 md:text-[52px]"
              dir="auto"
            >
              {name ? (
                <Trans>
                  Hi {name}, what can superchat{" "}
                  <strong className="font-bold text-foreground">build with you?</strong>
                </Trans>
              ) : (
                <Trans>
                  What can superchat{" "}
                  <strong className="font-bold text-foreground">build with you?</strong>
                </Trans>
              )}
            </h1>
            <div data-home-composer className="w-full max-w-[780px]">
              {composer}
            </div>
            {topics ? <div className="w-full max-w-[1120px] pb-20">{topics}</div> : null}
          </main>
        </div>
        <footer className="pointer-events-none relative z-10 flex flex-wrap items-end justify-between gap-4 md:absolute md:inset-x-8 md:bottom-7 [&>*]:pointer-events-auto">
          <SettingsButton onClick={onOpenSettings} />
          {recent}
        </footer>
      </div>
      {continuePill ? (
        <div className="pointer-events-none absolute inset-x-4 bottom-3 z-20 flex justify-center md:bottom-[22px]">
          {continuePill}
        </div>
      ) : null}
    </div>
  );
}
