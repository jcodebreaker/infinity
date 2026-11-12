/** Superchat ships in English only. */
export const UI_LOCALES = ["en"] as const;

export type UiLocale = (typeof UI_LOCALES)[number];

export const UI_LOCALE_STORAGE_KEY = "superchat.uiLocale";

export const UI_LOCALE_LABELS: Record<UiLocale, string> = {
  en: "English",
};

/** Return whether a value is one of the supported web UI locales. */
export function isUiLocale(value: string | null | undefined): value is UiLocale {
  return value === "en";
}

/** Every language tag resolves to English, the only UI locale. */
export function normalizeUiLocale(_raw: string | null | undefined): UiLocale {
  return "en";
}

function readStoredLocale(storage: Pick<Storage, "getItem"> | null | undefined): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(UI_LOCALE_STORAGE_KEY);
  } catch {
    return null;
  }
}

function readEnvDefault(env: { VITE_DEFAULT_UI_LOCALE?: string } | undefined): string | null {
  const value = env?.VITE_DEFAULT_UI_LOCALE;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readNavigatorLanguage(
  nav: Pick<Navigator, "language" | "languages"> | null | undefined,
): string | null {
  if (!nav) return null;
  if (typeof nav.language === "string" && nav.language) return nav.language;
  const first = nav.languages?.[0];
  return typeof first === "string" && first ? first : null;
}

export type ResolveUiLocaleOptions = {
  stored?: string | null;
  envDefault?: string | null;
  navigatorLanguage?: string | null;
  storage?: Pick<Storage, "getItem"> | null;
  env?: { VITE_DEFAULT_UI_LOCALE?: string };
  navigator?: Pick<Navigator, "language" | "languages"> | null;
};

/**
 * Order: saved choice (`localStorage`) → `VITE_DEFAULT_UI_LOCALE` →
 * `navigator.language` → English.
 */
export function resolveUiLocale(options: ResolveUiLocaleOptions = {}): UiLocale {
  const stored =
    options.stored !== undefined
      ? options.stored
      : readStoredLocale(
          options.storage ?? (typeof localStorage !== "undefined" ? localStorage : null),
        );
  if (stored) return normalizeUiLocale(stored);

  const envDefault =
    options.envDefault !== undefined
      ? options.envDefault
      : readEnvDefault(
          options.env ??
            (typeof import.meta !== "undefined"
              ? {
                  VITE_DEFAULT_UI_LOCALE: (import.meta as ImportMeta).env?.VITE_DEFAULT_UI_LOCALE,
                }
              : undefined),
        );
  if (envDefault) return normalizeUiLocale(envDefault);

  const navigatorLanguage =
    options.navigatorLanguage !== undefined
      ? options.navigatorLanguage
      : readNavigatorLanguage(
          options.navigator ?? (typeof navigator !== "undefined" ? navigator : null),
        );
  if (navigatorLanguage) return normalizeUiLocale(navigatorLanguage);

  return "en";
}

export function persistUiLocale(
  locale: UiLocale,
  storage: Pick<Storage, "setItem"> | null = typeof localStorage !== "undefined"
    ? localStorage
    : null,
): void {
  if (!storage) return;
  try {
    storage.setItem(UI_LOCALE_STORAGE_KEY, locale);
  } catch {
    // Ignore quota / private-mode failures; in-memory locale still applies.
  }
}
