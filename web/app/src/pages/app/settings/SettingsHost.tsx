import type { Bot } from "@superchat/contracts";
import { lazy } from "react";
import type { useShellSettings } from "./useShellSettings";

const SettingsOverlay = lazy(() =>
  import("../../SettingsOverlay").then((module) => ({ default: module.SettingsOverlay })),
);

/** The account Settings overlay, wired to the Shell's state. */
export function SettingsHost({
  settings,
  userName,
  email,
  active,
}: {
  settings: ReturnType<typeof useShellSettings>;
  userName: string;
  email: string | undefined;
  active: Bot | undefined;
}) {
  const { settingsOpen, setSettingsOpen, settingsSection, setSettingsSection } = settings;
  if (!settingsOpen) return null;
  return (
    <SettingsOverlay
      name={userName}
      email={email}
      initialSection={settingsSection}
      onClose={() => {
        setSettingsOpen(false);
        setSettingsSection("general");
      }}
      museBot={active ?? null}
    />
  );
}
