import { useLingui } from "@lingui/react/macro";
import { ORPCError } from "@orpc/client";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { PanelRowSkeletonList } from "../../components/chrome/PanelSkeleton";
import { type ClaimsWire, type MemoryClaim, MemorySections } from "./MemorySections";
import { addressMemoryItem, type MemoryProfileSection, parseMemoryProfile } from "./memoryProfile";

// `NOT_IMPLEMENTED`: `memory.profile` isn't served in this environment. `NOT_FOUND`: this
// Muse has no Conversation yet (a brand new Muse) — both read as "nothing remembered
// yet", never "Could not load…" (same reasoning as useActivities.ts's `isUnavailable`).
const isEmptyBackend = (error: unknown) =>
  error instanceof ORPCError && (error.code === "NOT_IMPLEMENTED" || error.code === "NOT_FOUND");

/** The Memory tab's wire: `rpc.memory.profile`, kept explicit (not the `rpc` client)
 * so the dev fixture page can supply a fake — same convention as `ActivityWire`
 * (ActivityRunDialog.tsx). */
export type MemoryWire = {
  profile: (input: { botId: string }) => Promise<{ profile: string | null }>;
} & Partial<ClaimsWire>;

type MemoryState =
  | { status: "loading" }
  | { status: "ready"; sections: MemoryProfileSection[]; claims?: MemoryClaim[] }
  | { status: "error" };

/**
 * The context panel's Memory tab: a read-only view of the Muse's Memory Profile
 * (`rpc.memory.profile`) — what it remembers about the person, in its own words, grouped
 * under the headings the engine gives it. When the claim wire is there, the tab shows the
 * editable sections instead (About you, Commitments, Projects & focus, People, How Superchat works
 * with you; see MemorySections.tsx). The Days section (daily notes, supplied through `days`) sits under them; Memory settings
 * (web/app/src/pages/app/settings/MemoryPanel.tsx) owns changing it.
 */
export function MemoryTab({
  botId,
  wire,
  days = null,
}: {
  botId: string;
  wire: MemoryWire;
  /** The daily-notes (Days) section, owned by the daily-notes feature and composed in by the
   * context panel; shown under the memory sections. */
  days?: ReactNode;
}) {
  const { t } = useLingui();
  const [state, setState] = useState<MemoryState>({ status: "loading" });
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setState({ status: "loading" });
    const load = wire.claims
      ? wire.claims({ botId }).then((result) => ({
          sections: [] as MemoryProfileSection[],
          claims: result.claims,
        }))
      : wire
          .profile({ botId })
          .then((result) => ({ sections: parseMemoryProfile(result.profile), claims: undefined }));
    void load
      .then((result) => {
        if (current !== generation.current) return;
        setState({ status: "ready", ...result });
      })
      .catch((error: unknown) => {
        if (current !== generation.current) return;
        setState(isEmptyBackend(error) ? { status: "ready", sections: [] } : { status: "error" });
      });
    return () => {
      generation.current += 1;
    };
  }, [botId, wire]);

  if (state.status === "loading") return <PanelRowSkeletonList count={3} />;
  if (state.status === "error") {
    return <p className="text-[13px] text-destructive">{t`Could not load Memory`}</p>;
  }
  const claimWire =
    wire.claims && wire.editClaim && wire.forgetClaim
      ? { claims: wire.claims, editClaim: wire.editClaim, forgetClaim: wire.forgetClaim }
      : null;
  if (state.claims && claimWire) {
    const claims = state.claims;
    return (
      <div className="flex flex-col gap-4" data-testid="memory-panel">
        {claims.length === 0 ? (
          <p className="py-8 text-center text-[12.5px] text-ink-3" data-testid="memory-empty">
            {t`Nothing remembered yet.`}
          </p>
        ) : (
          <MemorySections
            botId={botId}
            wire={claimWire}
            claims={claims}
            onChange={(next) => setState({ status: "ready", sections: [], claims: next })}
          />
        )}
        {days}
      </div>
    );
  }
  if (state.sections.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <p className="py-8 text-center text-[12.5px] text-ink-3" data-testid="memory-empty">
          {t`Nothing remembered yet.`}
        </p>
        {days}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="memory-panel">
      {state.sections.map((section, index) => (
        <section key={section.heading || index} className="flex flex-col gap-1">
          {section.heading ? (
            <h3 className="px-1.5 text-[13px] font-semibold text-foreground">{section.heading}</h3>
          ) : null}
          <ul className="superchat-group flex flex-col">
            {section.items.map((item, itemIndex) => (
              <li
                key={itemIndex}
                className="superchat-row superchat-row-plain px-3 py-2.5 text-[14px] leading-[1.4] text-foreground"
                dir="auto"
              >
                {addressMemoryItem(item)}
              </li>
            ))}
          </ul>
        </section>
      ))}
      {days}
    </div>
  );
}
