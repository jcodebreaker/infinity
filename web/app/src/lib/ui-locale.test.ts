import { describe, expect, it } from "vitest";
import { isUiLocale, normalizeUiLocale, resolveUiLocale, UI_LOCALES } from "./ui-locale";

describe("ui locale", () => {
  it("ships English only", () => {
    expect(UI_LOCALES).toEqual(["en"]);
    expect(isUiLocale("en")).toBe(true);
    expect(isUiLocale("fr")).toBe(false);
  });

  it("resolves every language tag to English", () => {
    for (const tag of ["fr-CA", "de", "zh-CN", "pt-BR", "", null, undefined]) {
      expect(normalizeUiLocale(tag)).toBe("en");
    }
  });

  it("stays English even when a saved choice, env default or browser language says otherwise", () => {
    expect(resolveUiLocale({ stored: "fr", envDefault: null, navigatorLanguage: null })).toBe("en");
    expect(resolveUiLocale({ stored: null, envDefault: "ko", navigatorLanguage: null })).toBe("en");
    expect(resolveUiLocale({ stored: null, envDefault: null, navigatorLanguage: "fr-CA" })).toBe(
      "en",
    );
  });
});
