import { useLingui } from "@lingui/react/macro";
import { useCallback } from "react";
import type { VersionOrigin } from "./thread";

/** The words for who made a version (ADR 0024's five origins): the thread's version markers and
 * the version menu read the same ones. */
export function useOriginLabel(): (origin: VersionOrigin) => string {
  const { t } = useLingui();
  return useCallback(
    (origin: VersionOrigin) => {
      switch (origin) {
        case "chat":
          return t`From this chat`;
        case "superchat":
          return t`From Super Chat`;
        case "you":
          return t`By you`;
        case "restored":
          return t`Restored`;
        case "updated":
          return t`Updated from source`;
      }
    },
    [t],
  );
}
