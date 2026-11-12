import { useLingui } from "@lingui/react/macro";

/** The line above the first proactive message the person had not seen. */
export function AwayDivider() {
  const { t } = useLingui();
  return (
    <div
      data-testid="away-divider"
      className="flex items-center gap-3 text-[12px] text-muted-foreground before:h-px before:flex-1 before:bg-border after:h-px after:flex-1 after:bg-border"
    >
      {t`While you were away`}
    </div>
  );
}
