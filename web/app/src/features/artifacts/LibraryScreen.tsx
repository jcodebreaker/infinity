import { Trans, useLingui } from "@lingui/react/macro";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@superchat/ui-web";
import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "../../components/ui";
import type { ArtifactKind } from "../../lib/artifact-kind";
import { artifactKind, KIND_ORDER, kindFacetLabel } from "../../lib/artifact-kind";
import { decodeArtifactBase64, downloadArtifactBytes } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";
import { notifyArtifactsChanged } from "./artifactChanges";
import { LibraryGrid, LibrarySkeletonGrid } from "./library/LibraryGrid";
import { type FilterChip, FilterChips, LibrarySearchField } from "./library/LibraryToolbar";
import type { ArtifactSummary } from "./library/types";
import { useArtifactLibraryFacets } from "./registry";

// An extension's own chip ("live") is any other string.
type Facet = ArtifactKind | "published" | "private" | "all" | (string & {});

const LIST_PAGE_SIZE = 60;
// Safety net against an unbounded fetch loop; a Library this size is not realistic.
const MAX_PAGES = 12;

const LIBRARY_SUGGESTIONS = [
  "Build me a simple expense tracker app",
  "Make a short deck about our new product",
];

/**
 * F7 · Library. Everything the Muse has made — apps, decks, documents, files — from the
 * Conversation and every Goal log, searchable in one grid. `artifacts.listSpace` is
 * space-scoped with an optional bot filter, not thread-scoped, and Goal-log turns
 * create artifacts under the same bot id as the Conversation, so they all show up
 * here too without any backend change.
 */
export function LibraryScreen({
  botId,
  onSendIdea,
  onOpenArtifact,
}: {
  botId: string;
  /** Starts a Conversation with a suggestion from the empty state. */
  onSendIdea?: (text: string) => void;
  /** Opens a file where the host shows it (the dock sheet swaps the grid for the editor). */
  onOpenArtifact: (artifactId: string) => void;
}) {
  const { t } = useLingui();
  const [items, setItems] = useState<ArtifactSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selectedKind, setSelectedKind] = useState<Facet>("all");
  const [pendingDelete, setPendingDelete] = useState<ArtifactSummary | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setLoadError(null);
    setSelectedKind("all");

    async function load() {
      let cursor: string | undefined;
      let all: ArtifactSummary[] = [];
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const result = await rpc.artifacts.listSpace({ botId, cursor, limit: LIST_PAGE_SIZE });
        if (cancelled) return;
        all = all.concat(result.items);
        setItems(all);
        if (!result.nextCursor) return;
        cursor = result.nextCursor;
      }
    }

    load().catch((error: unknown) => {
      if (cancelled) return;
      setItems((current) => current ?? []);
      setLoadError(error instanceof Error ? error.message : t`Could not load your Library.`);
    });

    return () => {
      cancelled = true;
    };
  }, [botId, t]);

  const facetCounts = useMemo(() => {
    const counts = new Map<ArtifactKind, number>();
    for (const item of items ?? []) {
      const kind = artifactKind(item.mimeType, item.name);
      counts.set(kind, (counts.get(kind) ?? 0) + 1);
    }
    return counts;
  }, [items]);

  const extraFacets = useArtifactLibraryFacets();
  const extraCounts = useMemo(
    () => extraFacets.map((facet) => (items ?? []).filter((item) => facet.matches(item)).length),
    [extraFacets, items],
  );
  const publishedCount = useMemo(() => items?.filter((item) => item.publish).length ?? 0, [items]);

  const filtered = useMemo(() => {
    if (!items) return null;
    const needle = query.trim().toLowerCase();
    return items.filter((item) => {
      const extra = extraFacets.find((facet) => facet.value === selectedKind);
      if (extra) {
        if (!extra.matches(item)) return false;
      } else if (selectedKind === "published") {
        if (!item.publish) return false;
      } else if (selectedKind === "private") {
        if (item.publish) return false;
      } else if (
        selectedKind !== "all" &&
        artifactKind(item.mimeType, item.name) !== selectedKind
      ) {
        return false;
      }
      if (!needle) return true;
      return (
        item.name.toLowerCase().includes(needle) ||
        (item.description?.toLowerCase().includes(needle) ?? false)
      );
    });
  }, [items, query, selectedKind, extraFacets]);

  async function handleDownload(item: ArtifactSummary) {
    try {
      const artifact = await rpc.artifacts.getById({ artifactId: item.id });
      downloadArtifactBytes(
        artifact.name,
        artifact.mimeType,
        decodeArtifactBase64(artifact.contentBase64),
      );
    } catch {
      // Best-effort; the person can try again from the card's "…" menu.
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await rpc.artifacts.remove({ artifactId: pendingDelete.id });
      notifyArtifactsChanged();
      setItems((current) => current?.filter((entry) => entry.id !== pendingDelete.id) ?? current);
      setPendingDelete(null);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t`Could not delete this.`);
    } finally {
      setDeleteBusy(false);
    }
  }

  const total = items?.length ?? 0;
  const visibleKinds = KIND_ORDER.filter((kind) => (facetCounts.get(kind) ?? 0) > 0);

  const facets: FilterChip<Facet>[] = [
    { value: "all", label: t`All`, count: total },
    ...visibleKinds.map((kind) => ({
      value: kind,
      label: kindFacetLabel(kind),
      count: facetCounts.get(kind) ?? 0,
    })),
    ...(publishedCount > 0
      ? [{ value: "published" as const, label: t`Published`, count: publishedCount }]
      : []),
    ...extraFacets.flatMap((facet, index) =>
      (extraCounts[index] ?? 0) > 0
        ? [{ value: facet.value, label: facet.label, count: extraCounts[index] ?? 0 }]
        : [],
    ),
  ];
  // A filter earns its place only when one of its choices shows less than "All" does.
  const kindControl = facets.some((facet) => facet.value !== "all" && facet.count < total) ? (
    <FilterChips label={t`Kind`} value={selectedKind} onChange={setSelectedKind} options={facets} />
  ) : null;
  const content = (
    <>
      {items === null ? (
        <LibrarySkeletonGrid />
      ) : items.length === 0 ? (
        loadError ? (
          <p className="py-10 text-[14px] text-destructive">{loadError}</p>
        ) : (
          <EmptyState
            illustration="books"
            headline={t`Nothing here yet.`}
            suggestions={LIBRARY_SUGGESTIONS}
            onSuggestion={onSendIdea}
          >
            {t`Apps, decks, documents and files your Muse makes will appear here.`}
          </EmptyState>
        )
      ) : filtered && filtered.length === 0 ? (
        <EmptyState headline={t`Nothing matches your search.`} />
      ) : (
        <LibraryGrid
          items={filtered ?? []}
          onOpen={(item) => onOpenArtifact(item.id)}
          onDownload={(item) => void handleDownload(item)}
          onDelete={setPendingDelete}
        />
      )}
    </>
  );
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5" data-testid="library-embedded">
      {total > 0 ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
          <LibrarySearchField value={query} onChange={setQuery} className="flex-1" />
          {kindControl}
        </div>
      ) : null}
      {content}
      {pendingDelete ? (
        <AlertDialog
          open
          onOpenChange={(open) => {
            if (!open && !deleteBusy) setPendingDelete(null);
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>
                <Trans>Delete "{pendingDelete.name}"?</Trans>
              </AlertDialogTitle>
              <AlertDialogDescription>
                {pendingDelete.versionCount > 1 ? (
                  <Trans>
                    This deletes all {pendingDelete.versionCount} versions of this. This can't be
                    undone.
                  </Trans>
                ) : (
                  <Trans>This can't be undone.</Trans>
                )}
              </AlertDialogDescription>
            </AlertDialogHeader>
            {deleteError ? <p className="text-[13.5px] text-destructive">{deleteError}</p> : null}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={deleteBusy}>
                <Trans>Cancel</Trans>
              </AlertDialogCancel>
              <AlertDialogAction
                variant="destructive"
                disabled={deleteBusy}
                onClick={() => void confirmDelete()}
              >
                {deleteBusy ? <Trans>Deleting…</Trans> : <Trans>Delete</Trans>}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      ) : null}
    </div>
  );
}
