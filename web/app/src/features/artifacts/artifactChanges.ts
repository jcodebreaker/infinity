// The app's signal that the person changed their files (deleted one, restored a version): views
// holding a list of files look again. Saves are heard from the transcript and the Activity feed.

let revision = 0;
const listeners = new Set<() => void>();

/** Tell the views listing files that the list moved on. */
export function notifyArtifactsChanged(): void {
  revision += 1;
  for (const listener of listeners) listener();
}

export function subscribeArtifactsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Moves on each time the files change: a dependency for a view's lookup. */
export function artifactsRevision(): number {
  return revision;
}
