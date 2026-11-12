import { Trans, useLingui } from "@lingui/react/macro";
import type { Bot } from "@superchat/contracts";
import { Button } from "@superchat/ui-web";
import { X } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import type { Panel } from "./panel";

/** The side panel's title row: what is open and the close button. */
export function SidePanelHeader({
  panel,
  setPanel,
  active,
}: {
  panel: Panel;
  setPanel: Dispatch<SetStateAction<Panel>>;
  active: Bot | undefined;
}) {
  const { t } = useLingui();
  return (
    <div className="mb-4 flex items-center justify-between">
      <span className="text-[13.5px] text-muted-foreground">
        {panel === "settings" ? <Trans>Settings</Trans> : active?.status}
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={t`Close panel`}
        onClick={() => setPanel(null)}
      >
        <X size={16} strokeWidth={1.8} />
      </Button>
    </div>
  );
}
