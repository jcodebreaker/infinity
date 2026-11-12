import { t } from "@lingui/core/macro";
import type { Artifact, ArtifactVersion } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { decodeArtifactBase64 } from "../../../lib/artifact-open";
import { rpc } from "../../../lib/rpc";
import { useVisiblePoll } from "../../../lib/use-visible-poll";

type ContentState =
  | { status: "loading" }
  | { status: "ready"; artifact: Artifact; bytes: Uint8Array }
  | { status: "error"; message: string };

type Loaded = ContentState & {
  /** Every version of the deliverable, newest first. */
  versions: ArtifactVersion[];
  /** The version currently shown (its artifact id). */
  versionId: string | null;
  selectVersion: (versionId: string) => void;
  /** A new version saved from the viewer (a sheet edit): show it and refresh the list without
   * blanking the viewer. */
  adoptVersion: (versionId: string) => void;
  /** A newer version than the one the person deliberately picked; null while following latest. */
  newer: ArtifactVersion | null;
  /** Jump to the newest version and follow it again. */
  showNewest: () => void;
  /** The newest version arrived while open (not the first load): who saved it, for a brief mark. */
  arrived: ArtifactVersion | null;
  /** Re-reads the version list now (a save was announced), following it as the poll does. */
  refresh?: () => void;
};

/** How often an open view re-reads its artifact's version list. */
const VERSION_POLL_MS = 4000;

/**
 * Loads one artifact's versions and the content of the chosen one (the latest until the person
 * picks another from the dialog's version menu).
 */
export function useArtifactContent(artifactId: string | null): Loaded {
  const [state, setState] = useState<ContentState>({ status: "loading" });
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  // The person chose a version from the menu: stay on it instead of following the newest.
  const pinned = useRef(false);
  const [arrived, setArrived] = useState<ArtifactVersion | null>(null);
  const pickedRef = useRef<string | null>(null);
  const versionsRef = useRef<ArtifactVersion[]>([]);

  const adoptVersion = useCallback(
    (versionId: string) => {
      pinned.current = false;
      setPicked(versionId);
      if (artifactId) {
        void rpc.artifacts
          .listVersions({ familyId: versionId })
          .then(setVersions)
          .catch(() => undefined);
      }
    },
    [artifactId],
  );

  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    setState({ status: "loading" });
    setPicked(null);
    pinned.current = false;
    setArrived(null);
    void rpc.artifacts
      .listVersions({ familyId: artifactId })
      .then((list) => {
        if (!cancelled) setVersions(list);
        return list;
      })
      .catch(() => [] as ArtifactVersion[])
      .then((list) => {
        if (!cancelled) setPicked(list[0]?.id ?? artifactId);
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  useEffect(() => {
    if (!picked) return;
    // Only the first load shows "loading" (set when the file changes): a newer version or one
    // picked from the menu replaces the one on screen once its bytes are here, so the editor and
    // the studio's conversation stay mounted (ADR 0030).
    let cancelled = false;
    void rpc.artifacts
      .getById({ artifactId: picked })
      .then((artifact) => {
        if (cancelled) return;
        setState({
          status: "ready",
          artifact,
          bytes: decodeArtifactBase64(artifact.contentBase64),
        });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : t`Could not load this.`,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [picked]);

  pickedRef.current = picked;
  versionsRef.current = versions;

  // Any open view follows new versions of its artifact: the engine's version list (keyed by the
  // artifact's family) is re-read while the tab is visible, then the view follows it unless pinned.
  const follow = useCallback(async () => {
    if (!artifactId) return;
    const list = await rpc.artifacts.listVersions({ familyId: artifactId });
    const top = list[0];
    if (!top || top.id === versionsRef.current[0]?.id) return;
    setVersions(list);
    if (pinned.current || pickedRef.current === top.id) return;
    setArrived(top);
    setPicked(top.id);
  }, [artifactId]);
  useVisiblePoll(follow, VERSION_POLL_MS, artifactId !== null && picked !== null);
  const refresh = useCallback(() => void follow().catch(() => undefined), [follow]);

  useEffect(() => {
    if (!arrived) return;
    const timer = window.setTimeout(() => setArrived(null), 6000);
    return () => window.clearTimeout(timer);
  }, [arrived]);

  const selectVersion = useCallback(
    (versionId: string) => {
      pinned.current = versionId !== versions[0]?.id;
      setArrived(null);
      setPicked(versionId);
    },
    [versions],
  );
  const newest = versions[0] ?? null;
  const newer = pinned.current && newest && newest.id !== picked ? newest : null;
  const showNewest = useCallback(() => {
    if (newest) selectVersion(newest.id);
  }, [newest, selectVersion]);

  return {
    ...state,
    versions,
    versionId: picked,
    selectVersion,
    adoptVersion,
    newer,
    showNewest,
    arrived,
    refresh,
  };
}
