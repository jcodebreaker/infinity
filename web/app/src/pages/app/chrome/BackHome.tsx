import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { cn, Tooltip, TooltipContent, TooltipTrigger } from "@superchat/ui-web";
import { ArrowUp } from "lucide-react";
import { SuperchatLogo } from "../../../components/SuperchatLogo";

/** The Conversation's logo is Home: it slides Home back (ADR 0035, amended). */
export function LogoHomeButton({ onHome, className }: { onHome: () => void; className?: string }) {
  const { t } = useLingui();
  return (
    <Tooltip>
      <TooltipTrigger
        aria-label={t`Home`}
        data-testid="logo-home"
        onClick={onHome}
        className={cn(
          "app-no-drag -m-1.5 rounded-[12px] p-1.5 transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-ring",
          className,
        )}
      >
        <SuperchatLogo />
      </TooltipTrigger>
      <TooltipContent side="bottom">{t`Home`}</TooltipContent>
    </Tooltip>
  );
}

/**
 * The slim tab at the top centre of the Conversation while the person pulls past its top:
 * "↑ Home · 3 topics". It fades in with the pull (shows at once under reduced motion) and
 * slides Home back when clicked.
 */
export function PullHomeTab({
  pull,
  topicCount,
  onHome,
}: {
  /** How far the pull has gone, 0 (hidden) to 1. */
  pull: number;
  topicCount: number;
  onHome: () => void;
}) {
  const { t } = useLingui();
  const shown = pull > 0;
  const topics = plural(topicCount, { one: "# topic", other: "# topics" });
  return (
    <div className="pointer-events-none absolute inset-x-0 top-2 z-20 flex justify-center">
      <button
        type="button"
        data-testid="pull-home"
        aria-hidden={!shown || undefined}
        tabIndex={shown ? 0 : -1}
        onClick={onHome}
        style={{ opacity: shown ? 0.45 + 0.55 * pull : 0 }}
        className={cn(
          "sc-glass flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[12.5px] font-medium text-foreground shadow-lg focus-visible:outline-2 focus-visible:outline-ring motion-safe:transition-opacity motion-safe:duration-200",
          // Reduced motion: no fade, the tab is simply there while pulling.
          shown && "pointer-events-auto motion-reduce:opacity-100!",
        )}
      >
        <ArrowUp aria-hidden="true" size={14} strokeWidth={2} />
        {topicCount > 0 ? t`Home · ${topics}` : t`Home`}
      </button>
    </div>
  );
}
