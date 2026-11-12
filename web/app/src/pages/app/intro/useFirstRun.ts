import { useCallback, useEffect, useState } from "react";
import { useUserId } from "../../../lib/use-user-id";
import { isFirstRunSeen, markFirstRunSeen } from "./firstRunStorage";

export type UseFirstRunResult = {
  /** Whether this person has already seen or dismissed this flag. */
  seen: boolean;
  /** Marks it seen; idempotent, safe to call more than once. */
  markSeen: () => void;
};

/**
 * One first-run flag (the welcome, or a one-time contextual hint), scoped to the signed-in
 * person (`useUserId`, the same source `Shell.tsx` reads for `userId`) and
 * persisted in `firstRunStorage`. Self-contained on purpose — every surface that shows a
 * first-run moment (`FirstRunHint`, the empty-Conversation welcome) just calls this with a
 * stable key instead of threading the person's id through props.
 */
export function useFirstRun(key: string): UseFirstRunResult {
  const userId = useUserId();
  const [seen, setSeen] = useState(() => isFirstRunSeen(userId, key));

  // The person's id can resolve after the first render; re-check once it does (and if the
  // hint's key ever changes under a stable component instance).
  useEffect(() => {
    setSeen(isFirstRunSeen(userId, key));
  }, [userId, key]);

  const markSeen = useCallback(() => {
    markFirstRunSeen(userId, key);
    setSeen(true);
  }, [userId, key]);

  return { seen, markSeen };
}
