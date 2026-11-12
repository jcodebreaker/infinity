import { useLingui } from "@lingui/react/macro";
import type { KnowledgeFileState } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { useKnowledgeAsleep, useKnowledgeFile } from "./status";

/** Where a file is in the index, as a Library card badge: reading it, searchable, or couldn't
 * be read. Nothing at all for a file the index doesn't hold. */
export function IndexBadge({ artifactId }: { artifactId: string }) {
  const { t } = useLingui();
  const file = useKnowledgeFile(artifactId);
  const asleep = useKnowledgeAsleep();
  if (!file) return null;
  const label: Record<KnowledgeFileState, string> = {
    indexing: t`Indexing…`,
    searchable: t`Searchable`,
    failed: t`Couldn't index`,
  };
  return (
    <span
      data-testid="library-card-index"
      data-state={file.state}
      className="superchat-glass-pill inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[11px] font-medium text-ink-2"
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full",
          file.state === "searchable" && "bg-success",
          file.state === "failed" && "bg-destructive",
          file.state === "indexing" && "bg-muted-foreground",
          file.state === "indexing" && !asleep && "animate-pulse",
        )}
      />
      {label[file.state]}
    </span>
  );
}
