import { Trans, useLingui } from "@lingui/react/macro";
import { ORPCError } from "@orpc/server";
import { Button, Skeleton } from "@superchat/ui-web";
import { Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { LoadingState } from "./components/ai/primitives";
import { loadMe, resetMe } from "./lib/bootstrap";
import { markAfterPaint, markOnce } from "./lib/performance";
import { ActivityPreviewPage } from "./pages/dev/ActivityPreviewPage";
import { AutomationsPreviewPage } from "./pages/dev/AutomationsPreviewPage";
import { CardsPreviewPage } from "./pages/dev/CardsPreviewPage";
import { DeckPreviewPage } from "./pages/dev/DeckPreviewPage";
import { MemoryPreviewPage } from "./pages/dev/MemoryPreviewPage";
import { SheetPreviewPage } from "./pages/dev/SheetPreviewPage";
import { StudioPreviewPage } from "./pages/dev/StudioPreviewPage";
import { ShellPage } from "./pages/Shell";

export function App() {
  // Dev-only fixture routes: no session needed, so screenshots and eyeballing don't require a
  // signed-in account or a live agent run. The clarification card and follow-up chips
  // (CardsPreviewPage.tsx).
  if (import.meta.env.DEV && window.location.pathname === "/dev/clarify") {
    return <CardsPreviewPage scenario="clarify" />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/looks") {
    return <CardsPreviewPage scenario="looks" />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/followups") {
    return <CardsPreviewPage scenario="followups" />;
  }
  // Dev-only fixture route for the Activity panel (ActivityPreviewPage.tsx): same
  // reasoning — `activities.*` isn't served by the local dev API either.
  if (import.meta.env.DEV && window.location.pathname === "/dev/activity") {
    return <ActivityPreviewPage />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/sheet") {
    return <SheetPreviewPage />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/deck") {
    return <DeckPreviewPage />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/studio") {
    return <StudioPreviewPage />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/automations") {
    return <AutomationsPreviewPage />;
  }
  if (import.meta.env.DEV && window.location.pathname === "/dev/memory") {
    return <MemoryPreviewPage />;
  }
  return <SessionApp />;
}

type MeGate = "loading" | "ready" | "unauthorized" | "unreachable";

function isUnauthorized(error: unknown) {
  return error instanceof ORPCError && error.code === "UNAUTHORIZED";
}

/** The engine's answer to "who is this?": the whole app waits on it. */
function useMeGate() {
  const [gate, setGate] = useState<MeGate>("loading");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void loadMe().then(
      () => {
        if (!cancelled) setGate("ready");
      },
      (error: unknown) => {
        if (!cancelled) setGate(isUnauthorized(error) ? "unauthorized" : "unreachable");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [attempt]);
  const retry = useCallback(() => {
    resetMe();
    setGate("loading");
    setAttempt((value) => value + 1);
  }, []);
  return { gate, retry };
}

function SessionApp() {
  const { gate, retry } = useMeGate();

  useLayoutEffect(() => {
    if (gate === "loading") return;
    markOnce("rk:renderer:session-committed");
    markAfterPaint("rk:renderer:session-painted");
  }, [gate]);

  if (gate === "unreachable") return <SessionUnavailable retry={retry} />;
  if (gate === "unauthorized") return <SignedOut />;
  if (gate === "loading") return <ShellSkeleton />;

  return (
    <div className="h-full" data-superchat-app-state="ready">
      <Suspense fallback={<div className="h-full bg-background" />}>
        <Routes>
          <Route path="/" element={<Navigate to="/app" replace />} />
          <Route path="/app" element={<ShellPage />} />
          <Route path="/app/:botId" element={<ShellPage />} />
        </Routes>
      </Suspense>
    </div>
  );
}

/** The engine answered 401: sign-in happens in front of superchat, so there is nothing to submit here. */
function SignedOut() {
  return (
    <div
      className="grid h-full place-items-center bg-background px-6 text-center"
      data-superchat-app-state="signed-out"
    >
      <p className="max-w-sm text-[15px] text-muted-foreground">
        <Trans>You're not signed in. Open superchat through your organisation's sign-in.</Trans>
      </p>
    </div>
  );
}

const RETRY_BASE_MS = 1_000;
const RETRY_MAX_MS = 15_000;

/**
 * A lookup that never reached the engine is not a sign-out, so the app waits and
 * retries here instead of stranding a signed-in person on an error.
 */
function SessionUnavailable({ retry }: { retry: () => void }) {
  const { t } = useLingui();
  const [attempt, setAttempt] = useState(0);
  const retryRef = useRef(retry);
  retryRef.current = retry;

  useEffect(() => {
    const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** attempt);
    const timer = setTimeout(() => retryRef.current(), delay);
    return () => clearTimeout(timer);
  }, [attempt]);

  return (
    <div className="grid h-full place-items-center bg-background px-6 text-center">
      <div className="flex flex-col items-center">
        <LoadingState label={t`Reconnecting`} />
        <p className="mt-3 text-[13.5px] text-muted-foreground/80">
          <Trans>Can&apos;t reach the server.</Trans>
        </p>
        <div className="mt-4">
          <Button
            variant="secondary"
            className="rounded-full"
            onClick={() => {
              setAttempt(0);
              retry();
            }}
          >
            <Trans>Retry now</Trans>
          </Button>
        </div>
      </div>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div
      className="flex h-full overflow-hidden bg-background"
      data-superchat-app-state="session-pending"
    >
      <aside className="hidden w-[316px] shrink-0 border-e border-sidebar-border bg-sidebar px-3.5 pt-16 md:block">
        <Skeleton className="h-10 rounded-xl" />
        <div className="mt-5 space-y-2 px-1">
          {[0, 1, 2, 3].map((row) => (
            <div key={row} className="flex items-center gap-3 rounded-xl px-2 py-2.5">
              <Skeleton className="size-9 rounded-full" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-3 w-2/5" />
                <Skeleton className="h-2.5 w-4/5" />
              </div>
            </div>
          ))}
        </div>
      </aside>
      <main className="flex flex-1 flex-col">
        <div className="h-[74px] border-b border-sidebar-border" />
        <div className="flex flex-1 items-center justify-center text-[14px] text-muted-foreground">
          <Trans>Opening superchat…</Trans>
        </div>
        <div className="mx-6 mb-6 h-[54px] rounded-full border border-border bg-background" />
      </main>
    </div>
  );
}
