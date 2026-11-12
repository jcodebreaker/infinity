import { useLingui } from "@lingui/react/macro";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@superchat/ui-web";
import { Ellipsis } from "lucide-react";
import type { ReactNode } from "react";

/** The header's `⋯` menu; its items come from the registry's `overflow` slot. */
export function PanelOverflowMenu({ children }: { children: ReactNode }) {
  const { t } = useLingui();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            aria-label={t`More actions`}
            data-testid="panel-overflow"
            className="grid size-8 place-items-center rounded-lg text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        }
      >
        <Ellipsis size={16} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">{children}</DropdownMenuContent>
    </DropdownMenu>
  );
}
