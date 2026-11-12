import { LiveMuseFaceProvider } from "@superchat/ui-web";
import { StrictMode, useEffect, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { BloopAvatar } from "./components/ai/bloop/BloopAvatar";
import { I18nBootstrap } from "./components/I18nBootstrap";
import { applyUiDirection } from "./lib/apply-ui-direction";
import { markAfterPaint, markOnce } from "./lib/performance";
import { installPreloadRecovery } from "./lib/preload-recovery";
import { applyMuseProductMode } from "./lib/product-mode";
import { applyUiAppearance, watchSystemAppearance } from "./lib/ui-appearance";
import { resolveUiLocale } from "./lib/ui-locale";
import "./styles.css";

// Warm the 3D Muse face while the app boots, so it is ready when the first face mounts.
function warmMuseFace() {
  void import("./components/ai/bloop/live");
  void import("three");
}
if (typeof window.requestIdleCallback === "function") {
  window.requestIdleCallback(warmMuseFace, { timeout: 1500 });
} else {
  globalThis.setTimeout(warmMuseFace, 200);
}

markOnce("rk:renderer:module-evaluated");
installPreloadRecovery();
applyUiDirection(resolveUiLocale());
applyUiAppearance();
applyMuseProductMode();

function PerformanceProbe() {
  useLayoutEffect(() => {
    markOnce("rk:renderer:first-react-commit");
    markAfterPaint("rk:renderer:first-react-painted");
  }, []);
  return null;
}

function AppearanceSync() {
  useEffect(() => watchSystemAppearance(), []);
  return null;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <PerformanceProbe />
    <AppearanceSync />
    <I18nBootstrap>
      {/* Router state updates must not be transitions: under sustained urgent
          updates (SSE churn while a run streams) a pending navigation is
          preempted indefinitely — useSearchParams/useParams then keep serving
          the stale location, so deep links (?m=) and thread switches never
          land while the URL already moved. */}
      <BrowserRouter useTransitions={false}>
        <LiveMuseFaceProvider value={BloopAvatar}>
          <App />
        </LiveMuseFaceProvider>
      </BrowserRouter>
    </I18nBootstrap>
  </StrictMode>,
);
