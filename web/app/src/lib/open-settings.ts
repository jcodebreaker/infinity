import { useEffect } from "react";

export type SettingsSection = "models";

const OPEN_SETTINGS_EVENT = "superchat:open-settings";

/** Asks the Shell to open Settings at a section, from anywhere (a note in the Conversation). */
export function requestOpenSettings(section: SettingsSection): void {
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: section }));
}

/** Calls `onOpen` when `requestOpenSettings` fires. */
export function useOpenSettingsRequests(onOpen: (section: SettingsSection) => void): void {
  useEffect(() => {
    const listener = (event: Event) => {
      const section = (event as CustomEvent<unknown>).detail;
      if (section === "models") onOpen(section);
    };
    window.addEventListener(OPEN_SETTINGS_EVENT, listener);
    return () => window.removeEventListener(OPEN_SETTINGS_EVENT, listener);
  }, [onOpen]);
}
