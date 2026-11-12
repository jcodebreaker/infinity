import { useLingui } from "@lingui/react/macro";
import { Button, cn, Dialog, DialogContent, DialogTitle } from "@superchat/ui-web";
import { BarChart3, Maximize2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useFileStudio } from "../../components/cards/context";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { decodeArtifactBase64 } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";

/** Until the page reports its height; about one KPI row and two rows of charts at chat width. */
const INITIAL_HEIGHT = 640;
/** A taller page scrolls inside the card; Open shows it whole. */
const MAX_HEIGHT = 1600;
/** A request for the page that gets no answer in this long fails, so it can be retried. */
const LOAD_TIMEOUT_MS = 20_000;

const FRAME = "min-w-0 rounded-2xl border border-border bg-card text-card-foreground";

const parseHtml = (bytes: Uint8Array) => {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
};

/** "failed" when the bytes are not UTF-8 text, "error" when the request failed (retryable). */
type PageState =
  | { status: "loading" }
  | { status: "failed" }
  | { status: "error" }
  | { status: "ready"; doc: string };

/** Pages already loaded this session, per artifact version, so a remount draws at once. */
const pages = new Map<string, string>();

/** The page's HTML, fetched once per version; `retry` fetches again after a failed request. */
function useDashboardHtml(artifactId: string, version?: number): [PageState, () => void] {
  const key = `${artifactId}:${version ?? ""}`;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<PageState>(() => {
    const cached = pages.get(key);
    return cached === undefined ? { status: "loading" } : { status: "ready", doc: cached };
  });
  useEffect(() => {
    const cached = pages.get(key);
    if (cached !== undefined) return setState({ status: "ready", doc: cached });
    let cancelled = false;
    setState({ status: "loading" });
    rpc.artifacts
      .getById({ artifactId }, { signal: AbortSignal.timeout(LOAD_TIMEOUT_MS) })
      .then((artifact) => parseHtml(decodeArtifactBase64(artifact.contentBase64)))
      .then(
        (doc): PageState => {
          if (doc === null) return { status: "failed" };
          pages.set(key, doc);
          return { status: "ready", doc };
        },
        (): PageState => ({ status: "error" }),
      )
      .then((next) => !cancelled && setState(next));
    return () => {
      cancelled = true;
    };
  }, [artifactId, key, attempt]);
  return [state, () => setAttempt((n) => n + 1)];
}

function currentTheme(): "light" | "dark" {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** The theme the app is in (its `data-theme`), kept current when the person switches it. */
function useAppTheme(): "light" | "dark" {
  const [theme, setTheme] = useState(currentTheme);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(currentTheme()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);
  return theme;
}

/** A page that did not load: its name, then Retry when the request failed, or Open (the studio's
 * own view of the file) when the file is not readable as a page. */
function LoadProblem({
  artifactId,
  name,
  onRetry,
}: {
  artifactId: string;
  name: string;
  onRetry?: () => void;
}) {
  const { t } = useLingui();
  const studio = useFileStudio();
  return (
    <div
      data-chart-state={onRetry ? "error" : "failed"}
      className={cn(FRAME, "flex items-center gap-3 px-4 py-3 text-[13.5px]")}
    >
      <BarChart3
        size={18}
        strokeWidth={1.6}
        className="shrink-0 text-muted-foreground"
        aria-hidden
      />
      <span className="min-w-0 flex-1 truncate" dir="auto">
        {name}
      </span>
      <span className="text-muted-foreground">
        {onRetry ? t`Couldn't load` : t`Dashboard unavailable`}
      </span>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t`Retry`}
        </Button>
      ) : studio ? (
        <Button variant="outline" size="sm" onClick={() => studio.open(artifactId)}>
          {t`Open`}
        </Button>
      ) : null}
    </div>
  );
}

/**
 * A dashboard page (`*.dashboard.html`, built by superchat-dashboard) in the chat: the live page at the
 * message's full width, sandboxed, as tall as it reports itself, with Open for the full view.
 */
export function DashboardPageResult({
  artifactId,
  name,
  version,
}: {
  artifactId: string;
  name: string;
  version?: number;
}) {
  const { t } = useLingui();
  const studio = useFileStudio();
  const [state, retry] = useDashboardHtml(artifactId, version);
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(INITIAL_HEIGHT);
  const [dialog, setDialog] = useState(false);
  // The page follows the app's theme, not the system's (the page reads `data-theme` first).
  const theme = useAppTheme();
  const html = useMemo(
    () =>
      state.status === "ready"
        ? `<script>document.documentElement.dataset.theme=${JSON.stringify(theme)}</script>${state.doc}`
        : "",
    [state, theme],
  );

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!frame.current || event.source !== frame.current.contentWindow) return;
      const data = event.data as { type?: unknown; height?: unknown } | null;
      if (data?.type !== "superchat:dashboard-height" || typeof data.height !== "number") return;
      if (!Number.isFinite(data.height) || data.height <= 0) return;
      setHeight(Math.min(Math.ceil(data.height), MAX_HEIGHT));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const title = name.replace(/\.dashboard\.html$/i, "");
  if (state.status === "failed" || state.status === "error") {
    return (
      <LoadProblem
        artifactId={artifactId}
        name={title}
        onRetry={state.status === "error" ? retry : undefined}
      />
    );
  }
  return (
    // A preferred width capped by the column: inside the w-fit message bubble an iframe has no
    // intrinsic width and would collapse.
    <article
      className={cn(FRAME, "group relative w-[48rem] max-w-full overflow-hidden")}
      data-dashboard-page={artifactId}
    >
      {state.status === "ready" ? (
        <div
          style={{ height }}
          className="transition-[height] duration-200 motion-reduce:transition-none"
        >
          <SandboxedHtmlViewer html={html} title={title} relay frameRef={frame} />
        </div>
      ) : (
        <div aria-busy="true" style={{ height }} className="animate-pulse bg-muted/50" />
      )}
      <div className="absolute end-2.5 top-2.5">
        <Button
          variant="outline"
          size="sm"
          onClick={() => (studio ? studio.open(artifactId) : setDialog(true))}
          aria-label={t`Open ${title}`}
          className="gap-1.5 rounded-lg bg-card/90 backdrop-blur"
        >
          <Maximize2 />
          {t`Open`}
        </Button>
      </div>
      {studio ? null : (
        <Dialog open={dialog} onOpenChange={setDialog}>
          <DialogContent className="h-[90vh] w-[min(1280px,96vw)] overflow-hidden rounded-2xl p-0 sm:max-w-none">
            <DialogTitle className="sr-only">{title}</DialogTitle>
            {dialog && html ? <SandboxedHtmlViewer html={html} title={title} /> : null}
          </DialogContent>
        </Dialog>
      )}
    </article>
  );
}
