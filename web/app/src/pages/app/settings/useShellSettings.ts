import { useState } from "react";
import { useOpenSettingsRequests } from "../../../lib/open-settings";
import type { SettingsSection } from "../../SettingsOverlay";

/** Which settings surfaces are open. */
export function useShellSettings() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  function openSettings(section: SettingsSection = "general") {
    setSettingsSection(section);
    setSettingsOpen(true);
  }
  // A note in the Conversation ("Add your API key") opens Settings at Models.
  useOpenSettingsRequests(openSettings);
  return {
    settingsOpen,
    setSettingsOpen,
    settingsSection,
    setSettingsSection,
    openSettings,
  };
}
