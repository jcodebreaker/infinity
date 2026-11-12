import type { Activity } from "@superchat/contracts";
import { useEffect, useMemo, useState } from "react";
import type { FirstMissing, StripHelper } from "./workStripState";
import { unlistedHelperIds } from "./workStripState";

const NONE: FirstMissing = new Map();

/** When each of these Helpers was first found missing from the loaded feed (client ms), held by
 * the view so a Helper the feed never lists settles after a grace (workStripState.ts). */
export function useFirstMissing(
  helpers: readonly StripHelper[],
  activities: readonly Activity[] | undefined,
): FirstMissing {
  const [seen, setSeen] = useState<FirstMissing>(NONE);
  const missing = useMemo(
    () => unlistedHelperIds(helpers, activities).join(" "),
    [helpers, activities],
  );
  useEffect(() => {
    if (!missing) return;
    setSeen((current) => {
      const added = missing.split(" ").filter((id) => !current.has(id));
      if (added.length === 0) return current;
      const next = new Map(current);
      const now = Date.now();
      for (const id of added) next.set(id, now);
      return next;
    });
  }, [missing]);
  return seen;
}
