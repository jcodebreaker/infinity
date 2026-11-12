import { useEffect, useState } from "react";
import { loadMe } from "./bootstrap";

/** The signed-in person's id once the engine has said who they are (undefined until then). */
export function useUserId(): string | undefined {
  const [userId, setUserId] = useState<string>();
  useEffect(() => {
    let cancelled = false;
    void loadMe().then(
      (me) => {
        if (!cancelled) setUserId(me.userId);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return userId;
}
