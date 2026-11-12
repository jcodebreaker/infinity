import { useEffect } from "react";
import { useOutboxUnread } from "./useOutboxUnread";

/** "(2) superchat" in the tab while proactive messages wait unseen; the page's own title else. */
export function useUnreadTitle(botId: string | null | undefined): void {
  const { count } = useOutboxUnread(botId);
  useEffect(() => {
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = count > 0 ? `(${count}) ${base}` : base;
    return () => {
      document.title = base;
    };
  }, [count]);
}
