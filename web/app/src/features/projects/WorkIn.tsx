import { Trans, useLingui } from "@lingui/react/macro";
import {
  Command,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
  cn,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@superchat/ui-web";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Folder,
  FolderOpen,
  Layers,
  SquareArrowOutUpRight,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import type { ChatProject } from "../../components/chrome/ProjectChip";

/** Where a chat works (docs/adr/0008): its Project, its folder relative to the workspace
 * (`null`: the whole workspace) and whether the person chose it. */
export type WorkPlace = { project: ChatProject | null; folder: string | null; chosen: boolean };

/** One workspace entry, as the Files browser lists it (`files.list`). */
export type FolderEntry = { name: string; path: string; type: "file" | "directory" };
/** Lists one workspace folder (`""`: the root). */
export type ListFolder = (path: string) => Promise<FolderEntry[]>;

const PROJECTS_DIR = "projects";
const PROJECT_CARD = "PROJECT.md";
/** Most Project folders checked for a card when the menu opens. */
const MAX_PROJECTS = 24;

/** Loads where a chat works; reloads when `refreshKey` changes (a turn ended) or on `reload`. */
export function useChatPlace(
  load:
    | (() => Promise<{ project: ChatProject | null; folder?: string | null; chosen?: boolean }>)
    | undefined,
  resetKey: string,
  refreshKey: string | number,
): { place: WorkPlace | null; reload: () => void } {
  const [state, setState] = useState<{ key: string; place: WorkPlace | null }>({
    key: resetKey,
    place: null,
  });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void load?.()
      .then(({ project, folder, chosen }) => {
        if (cancelled) return;
        setState({
          key: resetKey,
          place: { project, folder: folder ?? null, chosen: chosen ?? false },
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [resetKey, refreshKey, nonce]);
  return {
    place: state.key === resetKey ? state.place : null,
    reload: () => setNonce((value) => value + 1),
  };
}

const lastSegment = (path: string) => path.split("/").filter(Boolean).pop() ?? path;

/** The name a place goes by: its Project's card name, else its folder's own name. */
export function placeName(place: Pick<WorkPlace, "project" | "folder"> | null): string | null {
  if (place?.project) return place.project.name;
  return place?.folder ? lastSegment(place.folder) : null;
}

const isFolder = (entry: FolderEntry) => entry.type === "directory" && !entry.name.startsWith(".");
const byName = (a: FolderEntry, b: FolderEntry) =>
  a.name.localeCompare(b.name, undefined, { sensitivity: "base" });

/** Folders under `projects/` that carry a Project card. */
async function listProjects(list: ListFolder): Promise<FolderEntry[]> {
  const folders = (await list(PROJECTS_DIR).catch(() => [])).filter(isFolder).sort(byName);
  const checked = await Promise.all(
    folders.slice(0, MAX_PROJECTS).map(async (folder) => {
      const entries = await list(folder.path).catch(() => []);
      return entries.some((entry) => entry.name === PROJECT_CARD) ? folder : null;
    }),
  );
  return checked.filter((folder): folder is FolderEntry => folder !== null);
}

type Level =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; folders: FolderEntry[]; projects: FolderEntry[] };

/** Browses the workspace: the whole workspace, its Projects, and folders to drill into. */
function WorkInMenu({
  list,
  current,
  onPick,
  onShow,
  error,
}: {
  list: ListFolder;
  current: string | null;
  onPick: (folder: string | null) => void;
  onShow?: (folder: string) => void;
  error: boolean;
}) {
  const { t } = useLingui();
  const [dir, setDir] = useState("");
  const [query, setQuery] = useState("");
  const [level, setLevel] = useState<Level>({ status: "loading" });
  const cache = useRef(new Map<string, Level>());

  useEffect(() => {
    const cached = cache.current.get(dir);
    if (cached) {
      setLevel(cached);
      return;
    }
    let cancelled = false;
    setLevel({ status: "loading" });
    void Promise.all([list(dir), dir === "" ? listProjects(list) : Promise.resolve([])])
      .then(([entries, projects]) => {
        const folders = entries
          .filter(isFolder)
          .filter((entry) => dir !== "" || entry.name !== PROJECTS_DIR || projects.length === 0)
          .sort(byName);
        const next: Level = { status: "ready", folders, projects };
        cache.current.set(dir, next);
        if (!cancelled) setLevel(next);
      })
      .catch(() => {
        if (!cancelled) setLevel({ status: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [dir, list]);

  // The highlighted row: after drilling in, "Work in <folder>" so Enter picks it.
  const [selected, setSelected] = useState("");
  const open = (path: string) => {
    setQuery("");
    setDir(path);
    setSelected(path ? `work in ${path}` : "");
  };
  const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";
  const note = (text: ReactNode) => (
    <p className="px-3 py-4 text-center text-[13px] text-ink-3">{text}</p>
  );
  const folderRow = (entry: FolderEntry) => (
    <CommandItem
      key={entry.path}
      value={entry.path}
      keywords={[entry.name]}
      onSelect={() => open(entry.path)}
      className="min-h-9 gap-2.5 rounded-lg px-2.5 text-[13.5px]"
    >
      <Folder strokeWidth={1.7} className="text-ink-3" aria-hidden />
      <span className="truncate" dir="auto">
        {entry.name}
      </span>
      <CommandShortcut className="tracking-normal">
        <ChevronRight size={14} strokeWidth={1.8} className="text-ink-3" aria-hidden />
      </CommandShortcut>
    </CommandItem>
  );
  const pickRow = (folder: string | null, icon: ReactNode, label: ReactNode, value: string) => (
    <CommandItem
      value={value}
      data-checked={current === folder}
      onSelect={() => onPick(folder)}
      className="min-h-9 gap-2.5 rounded-lg px-2.5 text-[13.5px]"
    >
      {icon}
      <span className="truncate" dir="auto">
        {label}
      </span>
    </CommandItem>
  );

  return (
    <Command
      loop
      label={t`Choose where this chat works`}
      value={selected}
      onValueChange={setSelected}
      className="rounded-none! bg-transparent p-0"
      onKeyDown={(event) => {
        if (dir && !query && (event.key === "Backspace" || event.key === "ArrowLeft")) {
          event.preventDefault();
          open(parent);
        }
      }}
    >
      <CommandInput placeholder={t`Search folders`} value={query} onValueChange={setQuery} />
      <CommandList className="sc-scroll max-h-[min(52vh,360px)] p-1">
        {dir === "" ? (
          pickRow(
            null,
            <Layers strokeWidth={1.7} className="text-ink-3" aria-hidden />,
            <Trans>Whole workspace</Trans>,
            "whole workspace",
          )
        ) : (
          <>
            <CommandItem
              value="back"
              onSelect={() => open(parent)}
              className="min-h-9 gap-2 rounded-lg px-2.5 text-[13px] text-ink-2"
            >
              <ChevronLeft strokeWidth={1.8} aria-hidden />
              <span className="truncate" dir="auto">
                {parent ? lastSegment(parent) : t`Workspace`}
              </span>
            </CommandItem>
            {pickRow(
              dir,
              <FolderOpen strokeWidth={1.7} className="text-ink-3" aria-hidden />,
              <Trans>Work in {lastSegment(dir)}</Trans>,
              `work in ${dir}`,
            )}
          </>
        )}
        {level.status === "loading" ? note(<Trans>Loading…</Trans>) : null}
        {level.status === "error" ? note(<Trans>Couldn’t read the workspace.</Trans>) : null}
        {level.status === "ready" && level.projects.length > 0 ? (
          <CommandGroup heading={t`Projects`} className="px-0">
            {level.projects.map((project) =>
              pickRow(
                project.path,
                <Folder strokeWidth={1.7} className="text-ink-3" aria-hidden />,
                project.name,
                project.path,
              ),
            )}
          </CommandGroup>
        ) : null}
        {level.status === "ready" && level.folders.length > 0 ? (
          <CommandGroup heading={dir === "" ? t`Folders` : undefined} className="px-0">
            {level.folders.map(folderRow)}
          </CommandGroup>
        ) : null}
        {dir === "" && current !== null && onShow ? (
          <CommandGroup className="px-0">
            <CommandItem
              value="show in files"
              onSelect={() => onShow(current)}
              className="min-h-9 gap-2.5 rounded-lg px-2.5 text-[13px] text-ink-2"
            >
              <SquareArrowOutUpRight strokeWidth={1.7} aria-hidden />
              <Trans>Show in Files</Trans>
            </CommandItem>
          </CommandGroup>
        ) : null}
      </CommandList>
      {error ? (
        <p role="alert" className="px-3 pt-1 pb-2.5 text-[12.5px] text-destructive">
          <Trans>Couldn’t move this chat. Try again.</Trans>
        </p>
      ) : null}
    </Command>
  );
}

/**
 * "Work in": a quiet trigger and the folder menu it opens. `onPick` may be async (a move on the
 * engine); the menu closes once it settles and stays open with a note if it fails.
 */
export function WorkInPicker({
  list,
  current,
  onPick,
  onShow,
  testId,
  className,
  label,
  side = "bottom",
  align = "start",
}: {
  list: ListFolder;
  /** The chosen folder, workspace-relative; `null`: the whole workspace. */
  current: string | null;
  onPick: (folder: string | null) => Promise<unknown> | undefined;
  onShow?: (folder: string) => void;
  testId: string;
  className: string;
  label: ReactNode;
  side?: "top" | "bottom";
  align?: "start" | "center" | "end";
}) {
  const { t } = useLingui();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const pick = (folder: string | null) => {
    if (busy) return;
    if (folder === current) {
      setOpen(false);
      return;
    }
    const pending = onPick(folder);
    if (!pending) {
      setOpen(false);
      return;
    }
    setBusy(true);
    setError(false);
    void pending
      .then(() => setOpen(false))
      .catch(() => setError(true))
      .finally(() => setBusy(false));
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setError(false);
      }}
    >
      <PopoverTrigger
        type="button"
        data-testid={testId}
        aria-label={t`Choose where this chat works`}
        className={cn(className, busy && "opacity-60")}
      >
        {label}
      </PopoverTrigger>
      <PopoverContent
        side={side}
        align={align}
        sideOffset={8}
        className="sc-glass w-[min(320px,calc(100vw-2rem))] gap-0 overflow-hidden rounded-[18px] bg-window/95 p-1 shadow-float ring-0"
      >
        <WorkInMenu
          list={list}
          current={current}
          error={error}
          onPick={pick}
          onShow={
            onShow
              ? (folder) => {
                  setOpen(false);
                  onShow(folder);
                }
              : undefined
          }
        />
      </PopoverContent>
    </Popover>
  );
}

const CHIP =
  "app-no-drag pointer-events-auto inline-flex h-7 max-w-[min(280px,50vw)] items-center gap-1.5 rounded-full border px-2.5 text-[12.5px] shadow-sm backdrop-blur-md transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring data-[popup-open]:bg-accent data-[popup-open]:text-foreground";

/** The "Working in" chip of an existing chat: says where it works and moves it. With no folder,
 * a quiet "Work in…". */
export function WorkInChip({
  place,
  list,
  onPick,
  onShow,
  align = "start",
}: {
  place: WorkPlace | null;
  list: ListFolder;
  onPick: (folder: string | null) => Promise<unknown>;
  onShow?: (folder: string) => void;
  align?: "start" | "center" | "end";
}) {
  const name = placeName(place);
  return (
    <WorkInPicker
      list={list}
      current={place?.folder ?? null}
      testId="work-in-chip"
      align={align}
      onPick={onPick}
      onShow={onShow}
      className={cn(
        CHIP,
        name
          ? "border-border/60 bg-card/80 text-muted-foreground"
          : "border-transparent bg-transparent text-ink-3 shadow-none",
      )}
      label={
        <>
          <Folder size={13} strokeWidth={1.75} className="shrink-0" aria-hidden />
          <span className="truncate" dir="auto">
            {name ? <Trans>Working in {name}</Trans> : <Trans>Work in…</Trans>}
          </span>
        </>
      }
    />
  );
}
