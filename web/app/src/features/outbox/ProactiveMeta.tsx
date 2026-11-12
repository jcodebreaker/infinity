import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { useRef, useState } from "react";
import { rpc } from "../../lib/rpc";
import { useRhythmEvents } from "./useRhythmEvents";

type Proactive = NonNullable<ThreadMessage["proactive"]>;

const ACTION =
  "rounded-md px-1.5 py-0.5 transition-colors hover:bg-foreground/10 hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60";

/**
 * Under a message Superchat wrote on its own (ADR 0036): what brought it, in a quiet label, and
 * the two things the person can do about it. *Not useful* is stored; the line that replaces
 * the actions says so and nothing more. What the person does with the message is reported to
 * the rhythm (ADR 0038), invisibly.
 */
export function ProactiveMeta({
  botId,
  messageId,
  proactive,
}: {
  botId: string;
  messageId: string;
  proactive: Proactive;
}) {
  const { t } = useLingui();
  const [state, setState] = useState<"idle" | "busy" | "noted" | "failed">("idle");
  const root = useRef<HTMLDivElement>(null);
  useRhythmEvents(botId, messageId, root);
  const kind =
    // The Lookout's messages (ADR 0038); `topic_watch` is what it replaced, on older messages.
    proactive.kind === "lookout_wake" || proactive.kind === "topic_watch"
      ? t`Topic update`
      : proactive.kind === "daily_brief"
        ? t`Morning brief`
        : t`Automation`;
  const topicKey = proactive.topicKey;
  const run = (call: () => Promise<unknown>, done: "idle" | "noted") => {
    setState("busy");
    call().then(
      () => setState(done),
      () => setState("failed"),
    );
  };
  return (
    <div
      ref={root}
      data-testid="proactive-meta"
      className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3"
    >
      <span className="rounded-[5px] bg-foreground/[0.06] px-1.5 py-0.5 text-[10.5px] font-semibold tracking-[.04em] uppercase">
        {kind}
      </span>
      {state === "noted" ? (
        <span role="status">{t`Noted. You'll see less like this.`}</span>
      ) : (
        <>
          {topicKey ? (
            <button
              type="button"
              className={ACTION}
              disabled={state === "busy"}
              onClick={() =>
                run(() => rpc.outbox.openTopic({ botId, topicKey, itemId: messageId }), "idle")
              }
            >
              {t`Open topic`}
            </button>
          ) : null}
          <button
            type="button"
            className={ACTION}
            disabled={state === "busy"}
            onClick={() => run(() => rpc.outbox.notUseful({ botId, itemId: messageId }), "noted")}
          >
            {t`Not useful`}
          </button>
          {state === "failed" ? <span role="status">{t`That didn't go through.`}</span> : null}
        </>
      )}
    </div>
  );
}
