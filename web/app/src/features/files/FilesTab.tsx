import { Trans, useLingui } from "@lingui/react/macro";
import { ORPCError } from "@orpc/client";
import { cn, Input } from "@superchat/ui-web";
import { ChevronRight, Eye, EyeOff, File, Folder, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatRelativeTime } from "../../lib/relative-time";
import { rpc } from "../../lib/rpc";
import { FilePreviewDialog } from "./FilePreviewDialog";

type Entry = Awaited<ReturnType<typeof rpc.files.list>>["entries"][number];
type Dir =
  | { status: "loading" }
  | { status: "starting" }
  | { status: "error" }
  | { status: "ready"; entries: Entry[] };

// The host's runner can take a while to rebind after a restart: retry quietly meanwhile.
const START_RETRY_MS = 2500;
const START_RETRY_MAX = 40;
const HIDDEN_KEY = "superchat.files.showHidden";

const isStarting = (error: unknown) =>
  error instanceof ORPCError && error.code === "SERVICE_UNAVAILABLE";

/** Dot-prefixed at any depth, as in Omnigent's file panel. */
const isHidden = (path: string) => path.split("/").some((part) => part.startsWith("."));

function readShowHidden(): boolean {
  try {
    return localStorage.getItem(HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The Files tab: the Muse's workspace as a lazy-expanding tree, newest first,
 * with a filter and refresh. Opening a file shows the shared preview. `refreshKey` bumps when a
 * turn completes so the tree follows the Muse's work; `reveal` opens one folder (the Project chip).
 */
export function FilesTab({
  botId,
  refreshKey,
  reveal,
  selectedFolder,
  onSelectFolder,
}: {
  botId: string;
  refreshKey: number;
  /** A folder to open (and its parents), e.g. a Project; a new `nonce` re-opens it. */
  reveal?: { path: string; nonce: number } | null;
  /** The folder picked for "Chat with this folder", shown selected. */
  selectedFolder?: string | null;
  /** Clicking a folder also picks it. */
  onSelectFolder?: (path: string) => void;
}) {
  const { t } = useLingui();
  const [dirs, setDirs] = useState<Record<string, Dir>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(readShowHidden);
  const retries = useRef(new Map<string, { timer: number; attempts: number }>());
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const botRef = useRef(botId);
  botRef.current = botId;

  const clearRetries = useCallback(() => {
    for (const { timer } of retries.current.values()) window.clearTimeout(timer);
    retries.current.clear();
  }, []);

  const load = useCallback(
    async (path: string, quiet = false) => {
      const pending = retries.current.get(path);
      if (pending) window.clearTimeout(pending.timer);
      if (!quiet) setDirs((current) => ({ ...current, [path]: { status: "loading" } }));
      try {
        const { entries } = await rpc.files.list({ botId, path });
        if (botRef.current !== botId) return;
        retries.current.delete(path);
        setDirs((current) => ({ ...current, [path]: { status: "ready", entries } }));
      } catch (error) {
        if (botRef.current !== botId) return;
        const attempts = (retries.current.get(path)?.attempts ?? 0) + 1;
        if (isStarting(error) && attempts <= START_RETRY_MAX) {
          const timer = window.setTimeout(() => void load(path, true), START_RETRY_MS);
          retries.current.set(path, { timer, attempts });
          setDirs((current) =>
            current[path]?.status === "ready"
              ? current
              : { ...current, [path]: { status: "starting" } },
          );
          return;
        }
        retries.current.delete(path);
        setDirs((current) =>
          quiet && current[path]?.status === "ready"
            ? current
            : { ...current, [path]: { status: "error" } },
        );
      }
    },
    [botId],
  );

  const refresh = useCallback(() => {
    void load("", true);
    for (const path of expandedRef.current) void load(path, true);
  }, [load]);

  // A different Muse starts from its own root.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset only when the Muse changes
  useEffect(() => {
    clearRetries();
    setDirs({});
    setExpanded(new Set());
    setFilter("");
    setPreview(null);
    void load("");
    return clearRetries;
  }, [botId]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refresh only when the key bumps
  useEffect(() => {
    if (refreshKey > 0) refresh();
  }, [refreshKey]);

  useEffect(() => {
    if (!reveal) return;
    const parts = reveal.path.split("/");
    const folders = parts.map((_, index) => parts.slice(0, index + 1).join("/"));
    setExpanded((current) => new Set([...current, ...folders]));
    for (const folder of folders) void load(folder);
  }, [reveal?.nonce]);

  const toggle = (path: string) => {
    const open = expanded.has(path);
    setExpanded((current) => {
      const next = new Set(current);
      if (open) next.delete(path);
      else next.add(path);
      return next;
    });
    if (!open) void load(path);
  };

  const toggleHidden = () => {
    const next = !showHidden;
    setShowHidden(next);
    try {
      localStorage.setItem(HIDDEN_KEY, next ? "1" : "0");
    } catch {}
  };
  const visible = (entries: Entry[]) =>
    showHidden ? entries : entries.filter((entry) => !isHidden(entry.path));

  const root = dirs[""];
  const query = filter.trim().toLowerCase();

  const renderRow = (entry: Entry, depth: number, showPath = false) => {
    const isDir = entry.type === "directory";
    const open = expanded.has(entry.path);
    return (
      <li key={entry.path}>
        <button
          type="button"
          data-testid="file-row"
          aria-expanded={isDir ? open : undefined}
          aria-current={isDir && selectedFolder === entry.path ? "true" : undefined}
          onClick={() => {
            if (!isDir) return setPreview(entry.path);
            toggle(entry.path);
            onSelectFolder?.(entry.path);
          }}
          style={{ paddingInlineStart: `${depth * 14 + 8}px` }}
          className={cn(
            "flex w-full items-center gap-2 rounded-lg py-1.5 pe-2 text-start text-[13.5px] hover:bg-muted/70 focus-visible:bg-muted/70 focus-visible:outline-none",
            isDir && selectedFolder === entry.path && "bg-tint/20 text-foreground hover:bg-tint/25",
          )}
        >
          {isDir ? (
            <ChevronRight
              size={14}
              strokeWidth={1.8}
              className={cn(
                "shrink-0 text-muted-foreground transition-transform",
                open && "rotate-90",
              )}
              aria-hidden
            />
          ) : (
            <span className="w-3.5 shrink-0" aria-hidden />
          )}
          {isDir ? (
            <Folder
              size={15}
              strokeWidth={1.6}
              className="shrink-0 text-muted-foreground"
              aria-hidden
            />
          ) : (
            <File
              size={15}
              strokeWidth={1.6}
              className="shrink-0 text-muted-foreground"
              aria-hidden
            />
          )}
          <span className="min-w-0 flex-1 truncate" dir="auto" title={entry.path}>
            {showPath ? entry.path : entry.name}
          </span>
          <span className="shrink-0 text-[12px] tabular-nums text-muted-foreground">
            {formatSize(entry.size)}
          </span>
          <span className="w-[62px] shrink-0 text-end text-[12px] text-muted-foreground">
            {formatRelativeTime(new Date(entry.modifiedAt * 1000).toISOString())}
          </span>
        </button>
        {isDir && open ? renderDir(entry.path, depth + 1) : null}
      </li>
    );
  };

  const renderDir = (path: string, depth: number) => {
    const dir = dirs[path];
    if (dir?.status === "starting") {
      return (
        <p
          className="py-1.5 text-[12.5px] text-muted-foreground"
          style={{ paddingInlineStart: `${depth * 14 + 8}px` }}
        >
          <Trans>Starting your machine…</Trans>
        </p>
      );
    }
    if (!dir || dir.status === "loading") {
      return (
        <p
          className="py-1.5 text-[12.5px] text-muted-foreground"
          style={{ paddingInlineStart: `${depth * 14 + 8}px` }}
        >
          <Trans>Loading…</Trans>
        </p>
      );
    }
    if (dir.status === "error") {
      return (
        <p
          className="py-1.5 text-[12.5px] text-muted-foreground"
          style={{ paddingInlineStart: `${depth * 14 + 8}px` }}
        >
          <Trans>Could not load this.</Trans>{" "}
          <button type="button" onClick={() => void load(path)} className="underline">
            <Trans>Retry</Trans>
          </button>
        </p>
      );
    }
    const entries = visible(dir.entries);
    if (entries.length === 0) return null;
    return <ul>{entries.map((entry) => renderRow(entry, depth))}</ul>;
  };

  const loadedEntries = Object.values(dirs).flatMap((dir) =>
    dir.status === "ready" ? visible(dir.entries) : [],
  );
  const matches = query
    ? loadedEntries
        .filter((entry) => entry.path.toLowerCase().includes(query))
        .sort((a, b) => b.modifiedAt - a.modifiedAt)
    : [];

  return (
    <div data-testid="files-tab">
      <div className="mb-3 flex items-center gap-2">
        <Input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder={t`Filter files`}
          aria-label={t`Filter files`}
          className="h-8 flex-1 text-[13.5px]"
        />
        <button
          type="button"
          aria-label={showHidden ? t`Hide hidden files` : t`Show hidden files`}
          aria-pressed={showHidden}
          title={showHidden ? t`Hide hidden files` : t`Show hidden files`}
          onClick={toggleHidden}
          className="grid size-8 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          {showHidden ? (
            <EyeOff size={15} strokeWidth={1.7} />
          ) : (
            <Eye size={15} strokeWidth={1.7} />
          )}
        </button>
        <button
          type="button"
          aria-label={t`Refresh files`}
          onClick={refresh}
          className="grid size-8 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <RefreshCw size={15} strokeWidth={1.7} />
        </button>
      </div>
      {root?.status === "starting" ? (
        <p className="text-[13.5px] text-muted-foreground">
          <Trans>Starting your machine…</Trans>
        </p>
      ) : !root || root.status === "loading" ? (
        <p className="text-[13.5px] text-muted-foreground">
          <Trans>Loading…</Trans>
        </p>
      ) : root.status === "error" ? (
        <p className="text-[13.5px] text-muted-foreground">
          <Trans>Could not load this.</Trans>{" "}
          <button type="button" onClick={() => void load("")} className="underline">
            <Trans>Retry</Trans>
          </button>
        </p>
      ) : visible(root.entries).length === 0 ? (
        <p className="text-[13.5px] text-muted-foreground">
          <Trans>No files yet.</Trans>
        </p>
      ) : query ? (
        matches.length === 0 ? (
          <p className="text-[13.5px] text-muted-foreground">
            <Trans>No matches.</Trans>
          </p>
        ) : (
          <ul>{matches.map((entry) => renderRow(entry, 0, true))}</ul>
        )
      ) : (
        <ul>{visible(root.entries).map((entry) => renderRow(entry, 0))}</ul>
      )}
      {preview ? (
        <FilePreviewDialog
          botId={botId}
          path={preview}
          onOpenChange={(open) => {
            if (!open) setPreview(null);
          }}
        />
      ) : null}
    </div>
  );
}
