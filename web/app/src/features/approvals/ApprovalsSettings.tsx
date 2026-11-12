import { Trans, useLingui } from "@lingui/react/macro";
import type { ApprovalSpending, ApprovalStandingRule } from "@superchat/contracts";
import { Button } from "@superchat/ui-web";
import { useEffect, useId, useState } from "react";
import { MUSE_INSET_GROUP } from "../../components/ui";
import { rpc } from "../../lib/rpc";

const ROW = "flex min-h-[52px] items-center justify-between gap-4 px-4";

/**
 * What the Muse may do without asking: the "always allow" rules the person has set (each one
 * revocable) and the daily spending limit. Rules are only ever created from an approval card.
 */
export function ApprovalsSettings({ botId }: { botId: string }) {
  const { t } = useLingui();
  const capId = useId();
  const [rules, setRules] = useState<ApprovalStandingRule[] | null>(null);
  const [spending, setSpending] = useState<ApprovalSpending | null>(null);
  const [cap, setCap] = useState("0");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void rpc.approvals
      .rules({ botId })
      .then((next) => {
        if (cancelled) return;
        setRules(next.rules);
        setSpending(next.spending);
        setCap(String(next.spending.dailyCapUsd));
      })
      .catch(() => {
        if (!cancelled) setRules(null);
      });
    return () => {
      cancelled = true;
    };
  }, [botId]);

  if (!rules || !spending) return null;

  async function revoke(ruleId: string) {
    setError(null);
    try {
      await rpc.approvals.revoke({ botId, ruleId });
      setRules((current) => current?.filter((rule) => rule.id !== ruleId) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t`Could not revoke`);
    }
  }

  async function saveCap() {
    const value = Number(cap);
    if (!Number.isFinite(value) || value < 0 || value === spending?.dailyCapUsd) {
      setCap(String(spending?.dailyCapUsd ?? 0));
      return;
    }
    setError(null);
    try {
      const next = await rpc.approvals.setSpending({ botId, dailyCapUsd: value });
      setSpending(next);
      setCap(String(next.dailyCapUsd));
    } catch (err) {
      setCap(String(spending?.dailyCapUsd ?? 0));
      setError(err instanceof Error ? err.message : t`Could not save`);
    }
  }

  return (
    <section data-testid="approvals-settings">
      <h3 className="px-4 pb-2 text-[13.5px] text-muted-foreground">
        <Trans>Approvals</Trans>
      </h3>
      <div className={MUSE_INSET_GROUP}>
        <label htmlFor={capId} className={ROW}>
          <span className="text-[16px] text-foreground">
            <Trans>Daily spending limit</Trans>
          </span>
          <span className="flex items-center gap-1 text-[16px] text-foreground">
            $
            <input
              id={capId}
              inputMode="decimal"
              value={cap}
              onChange={(event) => setCap(event.target.value)}
              onBlur={() => void saveCap()}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
              className="w-20 rounded-lg bg-muted px-2.5 py-1.5 text-end tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </span>
        </label>
        {rules.map((rule) => (
          <div key={rule.id}>
            <div className="ms-4 border-t border-border/70" />
            <div className={ROW}>
              <span className="min-w-0 text-[15px] text-foreground" dir="auto">
                {rule.label}
              </span>
              <Button variant="ghost" onClick={() => void revoke(rule.id)}>
                <Trans>Revoke</Trans>
              </Button>
            </div>
          </div>
        ))}
      </div>
      {error ? (
        <p role="alert" className="px-4 pt-2 text-[13px] text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}
