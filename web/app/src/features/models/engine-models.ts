import { useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";

/** The engine's public code on an RPC error (`data.code`: `model_key_required`,
 * `account_suspended`, ...), when it has one. */
export function engineErrorCode(error: unknown): string | undefined {
  const code = (error as { data?: { code?: unknown } } | null)?.data?.code;
  return typeof code === "string" ? code : undefined;
}

export function formatUsd(value: number, locale: string): string {
  return new Intl.NumberFormat(locale || "en", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: Number.isInteger(value) ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(value);
}

/** The engine counts time in epoch seconds. */
export function formatEngineDate(seconds: number | null, locale: string): string {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleDateString(locale || "en", {
    month: "short",
    day: "numeric",
  });
}

type Status = Awaited<ReturnType<typeof rpc.engineModels.status>>;

/** Every mounted `useEngineModelsStatus`: a change saved in one place (Settings > Models) reaches
 * the others (the chat header). */
const listeners = new Set<() => void>();

/** Forget every listener (tests: a root a test left mounted never hears another's change). */
export function resetEngineModelsListeners(): void {
  listeners.clear();
}

/** Asks every mounted `useEngineModelsStatus` again. */
export function engineModelsChanged(): void {
  for (const listener of listeners) listener();
}

/** What the engine's model layer says about this person. `status` is `null` until the first
 * answer and when the engine cannot be asked (`settled` tells which); `enabled: false` means
 * Superchat runs without the engine. `refresh` asks again, here and wherever else it is used. */
export function useEngineModelsStatus(
  active = true,
): [status: Status | null, refresh: () => void, settled: boolean] {
  const [status, setStatus] = useState<Status | null>(null);
  const [settled, setSettled] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion((v) => v + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    void Promise.resolve()
      .then(() => rpc.engineModels.status())
      .then((next) => {
        if (cancelled) return;
        setStatus(next);
        setSettled(true);
      })
      .catch(() => {
        if (cancelled) return;
        setStatus(null);
        setSettled(true);
      });
    return () => {
      cancelled = true;
    };
  }, [active, version]);
  return [status, engineModelsChanged, settled];
}

export type ModelProvider = Awaited<ReturnType<typeof rpc.engineModels.providers>>[number];

/** The providers a key can be for: the engine's configured list, in its order. `null` until it
 * answers; empty when it cannot be asked. */
export function useModelProviders(): ModelProvider[] | null {
  const [providers, setProviders] = useState<ModelProvider[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve()
      .then(() => rpc.engineModels.providers())
      .then((next) => {
        if (!cancelled) setProviders(next);
      })
      .catch(() => {
        if (!cancelled) setProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return providers;
}
