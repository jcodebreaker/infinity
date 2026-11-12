import type { Artifact } from "@superchat/contracts";

/** Matches the shape `rpc.artifacts.listSpace` returns (Artifacts.tsx keeps its own copy). */
export type ArtifactSummary = Artifact & { versionCount: number };
