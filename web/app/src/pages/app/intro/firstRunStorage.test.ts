import { describe, expect, it, vi } from "vitest";
import { isFirstRunSeen, markFirstRunSeen, resetFirstRun } from "./firstRunStorage";

function stubStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("window", {
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, value);
      },
      removeItem: (key: string) => {
        store.delete(key);
      },
    },
  });
  return store;
}

describe("first-run flag persistence", () => {
  it("is unseen with no user and never touches storage", () => {
    stubStorage();
    expect(isFirstRunSeen(null, "welcome")).toBe(false);
    expect(isFirstRunSeen(undefined, "welcome")).toBe(false);
    markFirstRunSeen(null, "welcome");
    resetFirstRun(undefined);
    vi.unstubAllGlobals();
  });

  it("marks a flag seen, keyed per user", () => {
    stubStorage();
    expect(isFirstRunSeen("user-1", "welcome")).toBe(false);
    markFirstRunSeen("user-1", "welcome");
    expect(isFirstRunSeen("user-1", "welcome")).toBe(true);
    // A different flag, and a different user, are independent.
    expect(isFirstRunSeen("user-1", "goals-section")).toBe(false);
    expect(isFirstRunSeen("user-2", "welcome")).toBe(false);
    vi.unstubAllGlobals();
  });

  it("accumulates multiple seen flags for the same user", () => {
    stubStorage();
    markFirstRunSeen("user-1", "welcome");
    markFirstRunSeen("user-1", "proposal-card");
    expect(isFirstRunSeen("user-1", "welcome")).toBe(true);
    expect(isFirstRunSeen("user-1", "proposal-card")).toBe(true);
    expect(isFirstRunSeen("user-1", "goals-section")).toBe(false);
    vi.unstubAllGlobals();
  });

  it("resetFirstRun clears every flag for that user", () => {
    stubStorage();
    markFirstRunSeen("user-1", "welcome");
    markFirstRunSeen("user-1", "proposal-card");
    resetFirstRun("user-1");
    expect(isFirstRunSeen("user-1", "welcome")).toBe(false);
    expect(isFirstRunSeen("user-1", "proposal-card")).toBe(false);
    vi.unstubAllGlobals();
  });

  it("survives a localStorage that throws", () => {
    vi.stubGlobal("window", {
      localStorage: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
        removeItem: () => {
          throw new Error("blocked");
        },
      },
    });
    expect(() => isFirstRunSeen("user-1", "welcome")).not.toThrow();
    expect(isFirstRunSeen("user-1", "welcome")).toBe(false);
    expect(() => markFirstRunSeen("user-1", "welcome")).not.toThrow();
    expect(() => resetFirstRun("user-1")).not.toThrow();
    vi.unstubAllGlobals();
  });
});
