export const SLASH_ACTIONS = [
  { id: "chat-settings" as const, label: "Chat Settings" },
  { id: "settings-general" as const, label: "Settings: General" },
] as const;

export type SlashActionId = (typeof SLASH_ACTIONS)[number]["id"];
