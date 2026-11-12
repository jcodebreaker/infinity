import { useLingui } from "@lingui/react/macro";
import type { Bot } from "@superchat/contracts";
import { Button, Dialog, DialogClose, DialogContent, DialogTitle } from "@superchat/ui-web";
import { Cpu, Monitor, Settings, UserRound, XIcon } from "lucide-react";
import { type ComponentType, useEffect, useRef, useState } from "react";
import { SuperchatTile, type TileTone } from "../components/chrome/SuperchatTile";
import { ModelsPanel, useEngineModelsStatus } from "../features/models";
import { SuperchatSettingsPanel } from "./app/SuperchatSettingsPanel";
import { GeneralPanel } from "./app/settings/GeneralPanel";
import { HostStatusPanel } from "./app/settings/HostStatusPanel";

export type SettingsSection = "superchat" | "general" | "models" | "machine";

/** Superchat's Settings sidebar: one colored tile per section, Mac style. */
const SETTINGS_TONE: Record<SettingsSection, TileTone> = {
  superchat: "blue",
  general: "gray",
  machine: "blue",
  models: "indigo",
};

type NavItem = {
  id: SettingsSection;
  label: string;
  icon: ComponentType<{ className?: string; strokeWidth?: number }>;
};

export function SettingsOverlay({
  email,
  name,
  initialSection = "general",
  onClose,
  museBot,
}: {
  email?: string | null;
  name: string;
  initialSection?: SettingsSection;
  onClose: () => void;
  /** The person's one Muse; the Superchat and Machine sections need it. */
  museBot?: Bot | null;
}) {
  const { t } = useLingui();
  const panelRef = useRef<HTMLDivElement>(null);
  const [section, setSection] = useState<SettingsSection>(initialSection);

  useEffect(() => {
    setSection(initialSection);
  }, [initialSection]);

  // Models is the engine's: without it there is no such section.
  const [engineModels, refreshEngineModels, engineSettled] = useEngineModelsStatus(true);
  const showEngineModels = engineModels?.enabled === true;

  useEffect(() => {
    if (!engineSettled) return;
    if (section === "models" && !showEngineModels) setSection("general");
  }, [section, engineSettled, showEngineModels]);

  const navItems: NavItem[] = [
    ...(museBot
      ? [{ id: "superchat" as const, label: museBot.name || t`superchat`, icon: UserRound }]
      : []),
    { id: "general", label: t`General`, icon: Settings },
    ...(showEngineModels ? [{ id: "models" as const, label: t`Models`, icon: Cpu }] : []),
    ...(museBot ? [{ id: "machine" as const, label: t`Machine`, icon: Monitor }] : []),
  ];

  const sectionTitle =
    navItems.find((item) => item.id === section)?.label ??
    (section === "general" ? t`General` : t`Settings`);

  const closeLabel = section === "models" ? t`Close model settings` : t`Close user settings`;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        ref={panelRef}
        data-testid="user-settings"
        data-settings-section={section}
        showCloseButton={false}
        initialFocus={() => panelRef.current}
        className="superchat-window flex h-[min(720px,calc(100%-2rem))] max-h-[calc(100%-2rem)] w-[min(920px,calc(100%-2rem))] flex-col gap-0 overflow-hidden rounded-[22px] p-0 ring-0 sm:max-h-[calc(100%-5rem)] sm:max-w-[920px]"
      >
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <nav
            data-testid="settings-nav"
            aria-label={t`Settings`}
            className="superchat-glass flex shrink-0 flex-row gap-1 overflow-x-auto overscroll-x-contain border-b border-border px-3 py-3 max-sm:justify-between md:m-2 md:w-[200px] md:flex-col md:gap-px md:overflow-y-auto md:rounded-[18px] md:border-b-0 md:border-e-0 md:px-2 md:py-4"
          >
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = item.id === section;
              return (
                <button
                  key={item.id}
                  type="button"
                  data-testid={`settings-nav-${item.id}`}
                  aria-current={active ? "page" : undefined}
                  onClick={() => setSection(item.id)}
                  className={`flex h-8 shrink-0 items-center gap-2.5 rounded-lg px-2 text-start text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-ring ${
                    active ? "bg-tint font-medium text-white" : "text-foreground hover:bg-selection"
                  }`}
                >
                  <SuperchatTile tone={SETTINGS_TONE[item.id]} size={22} className="max-sm:hidden">
                    <Icon strokeWidth={2.4} />
                  </SuperchatTile>
                  <span className="whitespace-nowrap">{item.label}</span>
                </button>
              );
            })}
          </nav>

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex items-start justify-between gap-4 px-6 pt-6 sm:px-8 sm:pt-7">
              <DialogTitle className="text-[28px] font-bold tracking-[0.2px] text-foreground">
                {sectionTitle}
              </DialogTitle>
              <DialogClose
                aria-label={closeLabel}
                render={<Button variant="ghost" size="icon-sm" />}
              >
                <XIcon />
              </DialogClose>
            </div>

            <div className="sc-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 pb-6 pt-5 sm:px-8 sm:pb-8">
              {section === "superchat" && museBot ? (
                <SuperchatSettingsPanel bot={museBot} userId={email ?? undefined} />
              ) : null}
              {section === "general" ? <GeneralPanel name={name} email={email} /> : null}
              {section === "machine" && museBot ? (
                <div className="space-y-5">
                  <HostStatusPanel botId={museBot.id} />
                </div>
              ) : null}
              {section === "models" && engineModels?.enabled ? (
                <ModelsPanel status={engineModels} onChanged={refreshEngineModels} />
              ) : null}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
