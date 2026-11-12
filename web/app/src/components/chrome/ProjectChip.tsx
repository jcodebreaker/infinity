import { Trans } from "@lingui/react/macro";
import { Folder } from "lucide-react";
import { useEffect, useState } from "react";

/** The Project a chat has open (docs/adr/0008). */
export type ChatProject = { slug: string; name: string };

/** Loads the Project a chat is working in; reloads when `refreshKey` changes (a turn ended). */
export function useChatProject(
  load: (() => Promise<{ project: ChatProject | null }>) | undefined,
  resetKey: string,
  refreshKey: string | number,
): ChatProject | null {
  const [state, setState] = useState<{ key: string; project: ChatProject | null }>({
    key: resetKey,
    project: null,
  });
  // biome-ignore lint/correctness/useExhaustiveDependencies: `load` is rebuilt every render; the keys say when to reload
  useEffect(() => {
    let cancelled = false;
    void load?.()
      .then(({ project }) => {
        if (!cancelled) setState({ key: resetKey, project });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [resetKey, refreshKey]);
  return state.key === resetKey ? state.project : null;
}

/** A quiet "Working in" note; it opens the Project's folder in the Files tab. */
export function ProjectChip({
  project,
  onOpen,
}: {
  project: ChatProject;
  onOpen: (project: ChatProject) => void;
}) {
  const { name } = project;
  return (
    <button
      type="button"
      data-testid="project-chip"
      onClick={() => onOpen(project)}
      className="app-no-drag pointer-events-auto inline-flex h-7 max-w-[min(280px,50vw)] items-center gap-1.5 rounded-full border border-border/60 bg-card/80 px-2.5 text-[12.5px] text-muted-foreground shadow-sm backdrop-blur-md transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
    >
      <Folder size={13} strokeWidth={1.75} className="shrink-0" aria-hidden />
      <span className="truncate" dir="auto">
        <Trans>Working in {name}</Trans>
      </span>
    </button>
  );
}
