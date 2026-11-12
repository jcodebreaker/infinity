import { useLingui } from "@lingui/react/macro";
import type { Artifact } from "@superchat/contracts";
import { Checkbox, Label } from "@superchat/ui-web";
import { useEffect, useState } from "react";
import type {
  ArtifactCardParts,
  ArtifactExtension,
  ArtifactPanelArgs,
  ArtifactPanelParts,
  LibraryFacet,
} from "../artifacts";
import {
  artifactKey,
  focusArtifactPart,
  setArtifactMarked,
} from "../artifacts/editor/editor-state";
import { LiveBar, whenText } from "./LiveBar";
import { LiveSettings } from "./LiveSettings";
import { canBeLive, isLiveSheet, useLive } from "./useLive";
import { type LivePageHealth, livePageHealth, useLivePages } from "./useLivePages";

/** The artifact panel's live parts: tag, "Keep updated" button, and the update bar. */
function useLivePanel({ artifact, newestVersion }: ArtifactPanelArgs): ArtifactPanelParts {
  const { t } = useLingui();
  const api = useLive(artifact);
  const { live } = api;
  const [showChanges, setShowChanges] = useState(true);
  const key = artifact ? artifactKey(artifact) : "";
  const newest = !!live?.lastUpdate && live.lastUpdate.version === artifact?.version;

  // The parts the newest update changed (and the ones in dispute) are marked in the page.
  const marked = [
    ...(newest ? (live?.lastUpdate?.applied ?? []).map((change) => change.id) : []),
    ...(live?.lastUpdate?.suggestions ?? []).map((suggestion) => suggestion.id),
  ].join("\n");
  useEffect(() => {
    if (!key) return;
    setArtifactMarked(key, showChanges && marked ? marked.split("\n") : []);
    return () => setArtifactMarked(key, []);
  }, [key, showChanges, marked]);

  if (!canBeLive(artifact) || live === undefined) return {};
  const updatedAt = live?.lastUpdate?.at ?? live?.checkedAt;
  return {
    badge: live ? (
      <span
        data-testid="live-tag"
        className="shrink-0 text-[12px] text-muted-foreground"
        title={updatedAt ? t`Updated ${whenText(updatedAt)}` : undefined}
      >
        {t`Live`}
      </span>
    ) : null,
    versionNote:
      live?.checkedAt &&
      newestVersion &&
      live.lastUpdate?.version !== newestVersion.version &&
      live.checkedAt * 1000 > Date.parse(newestVersion.createdAt)
        ? t`Last checked ${whenText(live.checkedAt)} · nothing new`
        : null,
    actions: <LiveSettings api={api} live={live} />,
    below: live ? (
      <LiveBar
        live={live}
        api={api}
        newest={newest}
        showChanges={showChanges}
        onShowChanges={setShowChanges}
        onPick={(id) => focusArtifactPart(key, id)}
      />
    ) : null,
  };
}

/** The Library tag of a live page: "Live", "Live · update failed" or "Live · paused". */
function useLiveCard(artifact: Artifact): ArtifactCardParts | null {
  const { t } = useLingui();
  const pages = useLivePages();
  const page = pages?.find((entry) => entry.artifactId === artifact.id);
  if (!page) return null;
  const health = livePageHealth(page);
  const label: Record<LivePageHealth, string> = {
    ok: t`Live`,
    failed: t`Live · update failed`,
    paused: t`Live · paused`,
  };
  return {
    badge: (
      <span
        data-testid="live-badge"
        data-health={health}
        className={`inline-flex shrink-0 items-center rounded-full bg-popover/85 px-2 py-0.5 text-[11px] font-medium shadow-xs ring-1 ring-border backdrop-blur-sm ${health === "ok" ? "text-foreground" : "text-destructive"}`}
      >
        {label[health]}
      </span>
    ),
  };
}

/** The Library's "Live" filter chip, once at least one page is live. */
function useLiveFacet(): LibraryFacet | null {
  const { t } = useLingui();
  const pages = useLivePages();
  if (!pages?.length) return null;
  const ids = new Set(pages.map((page) => page.artifactId));
  return { value: "live", label: t`Live`, matches: (artifact) => ids.has(artifact.id) };
}

/** In the publish dialog: the published copy follows updates (only for a page that is live). */
function FollowUpdates({
  artifact,
  register,
}: {
  artifact: Artifact;
  register: Parameters<NonNullable<ArtifactExtension["publishOptions"]>>[1];
}) {
  const { t } = useLingui();
  const api = useLive(artifact);
  const { live } = api;
  const [follow, setFollow] = useState<boolean | null>(null);
  const checked = follow ?? live?.followPublish ?? false;
  useEffect(() => {
    if (!live?.schedule || follow === null || follow === live.followPublish) return;
    const { sources, tell } = live;
    const schedule = live.schedule;
    return register(async () => {
      await api.save({ schedule, sources, tell, followPublish: follow });
    });
  }, [live, follow, register, api.save]);
  if (!live?.schedule) return null;
  return (
    <Label className="gap-2 text-[13px] font-normal">
      <Checkbox checked={checked} onCheckedChange={(on) => setFollow(on === true)} />
      {t`Published copy follows updates`}
    </Label>
  );
}

/** Keeps a page up to date on a schedule; the editor shell, the version menu and the publish dialog
 * show it through the registry's slots. */
export const liveExtension: ArtifactExtension = {
  usePanel: useLivePanel,
  card: () => null,
  useCard: useLiveCard,
  useLibraryFacet: useLiveFacet,
  publishOptions: (artifact, register) =>
    canBeLive(artifact) && !isLiveSheet(artifact) ? (
      <FollowUpdates artifact={artifact} register={register} />
    ) : null,
};
