import { Trans, useLingui } from "@lingui/react/macro";
import { useCallback, useEffect, useId, useState } from "react";
import { SettingsGroup, SettingsRow, SettingsSegmented } from "../../components/settings/kit";
import { errorText } from "../../lib/error-text";
import { rpc } from "../../lib/rpc";
import { formatUsd } from "./engine-models";

type BudgetView = {
  monthlyLimitUsd: number | null;
  atLimit: "stop" | "ask";
  spentMonthUsd: number;
  orgLimitUsd?: number | null;
};

/** Month-to-date spend against a limit: a quiet bar that turns to the alert tone at the limit. */
export function SpendBar({ spent, limit }: { spent: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(spent / limit, 1) : 0;
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={limit}
      aria-valuenow={Math.min(spent, limit)}
      className="h-1.5 w-full overflow-hidden rounded-full bg-selection"
    >
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${
          spent >= limit ? "bg-destructive" : "bg-tint"
        }`}
        style={{ width: `${Math.round(ratio * 100)}%` }}
      />
    </div>
  );
}

/**
 * A monthly model budget: the limit (empty is none), what happens at the limit, and the month
 * so far. The person also sees the organization's cap, read-only. The engine owns the meaning of
 * "the limit"; this only states the intent.
 */
export function BudgetGroup() {
  const { t, i18n } = useLingui();
  const limitId = useId();
  const [budget, setBudget] = useState<BudgetView | null>(null);
  const [limit, setLimit] = useState("");
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((next: BudgetView) => {
    setBudget(next);
    setLimit(next.monthlyLimitUsd === null ? "" : String(next.monthlyLimitUsd));
  }, []);

  useEffect(() => {
    let cancelled = false;
    void rpc.engineModels
      .budget()
      .then((next) => {
        if (!cancelled) apply(next);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [apply]);

  if (!budget) return null;

  async function save(next: { monthlyLimitUsd: number | null; atLimit: "stop" | "ask" }) {
    setError(null);
    try {
      apply(await rpc.engineModels.setBudget(next));
    } catch (err) {
      setError(errorText(err, t`Could not save the budget.`));
      if (budget) setLimit(budget.monthlyLimitUsd === null ? "" : String(budget.monthlyLimitUsd));
    }
  }

  function commitLimit() {
    if (!budget) return;
    const text = limit.trim().replace(/^\$/, "");
    const value = text === "" ? null : Number(text);
    if (value !== null && (!Number.isFinite(value) || value < 0)) {
      setLimit(budget.monthlyLimitUsd === null ? "" : String(budget.monthlyLimitUsd));
      return;
    }
    if (value === budget.monthlyLimitUsd) return;
    void save({ monthlyLimitUsd: value, atLimit: budget.atLimit });
  }

  const locale = i18n.locale;
  const spent = formatUsd(budget.spentMonthUsd, locale);
  return (
    <SettingsGroup title={<Trans>Budget</Trans>}>
      <label htmlFor={limitId} className="contents">
        <SettingsRow label={<Trans>Monthly limit</Trans>}>
          <span className="flex items-center gap-1 text-[14px] text-foreground">
            $
            <input
              id={limitId}
              inputMode="decimal"
              value={limit}
              placeholder={t`No limit`}
              onChange={(event) => setLimit(event.target.value)}
              onBlur={commitLimit}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
              className="w-24 rounded-lg bg-muted px-2.5 py-1.5 text-end tabular-nums outline-none placeholder:text-ink-3 focus-visible:ring-2 focus-visible:ring-ring"
            />
          </span>
        </SettingsRow>
      </label>
      <SettingsSegmented
        label={t`At the limit`}
        testId="budget-at-limit"
        value={budget.atLimit}
        options={[
          { value: "ask", label: t`Ask me` },
          { value: "stop", label: t`Stop` },
        ]}
        onChange={(atLimit) => void save({ monthlyLimitUsd: budget.monthlyLimitUsd, atLimit })}
      />
      <div className="flex flex-col gap-2 px-3 py-3" data-testid="budget-spend">
        <div className="flex items-baseline justify-between text-[13.5px]">
          <span className="text-foreground">
            <Trans>Spent this month</Trans>
          </span>
          <span className="tabular-nums text-ink-3">
            {budget.monthlyLimitUsd !== null
              ? t`${spent} of ${formatUsd(budget.monthlyLimitUsd, locale)}`
              : spent}
          </span>
        </div>
        {budget.monthlyLimitUsd !== null ? (
          <SpendBar spent={budget.spentMonthUsd} limit={budget.monthlyLimitUsd} />
        ) : null}
      </div>
      {budget.orgLimitUsd != null ? (
        <SettingsRow
          label={<Trans>Organization limit</Trans>}
          value={formatUsd(budget.orgLimitUsd, locale)}
        />
      ) : null}
      {error ? (
        <p role="alert" className="px-3 pb-2 text-[12.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </SettingsGroup>
  );
}
