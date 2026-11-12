import { Trans, useLingui } from "@lingui/react/macro";
import { Button } from "@superchat/ui-web";
import { useEffect, useState } from "react";
import { hostOf } from "../../components/cards/links";
import { MUSE_INSET_GROUP } from "../../components/ui";
import { rpc } from "../../lib/rpc";

type VaultEntry = Awaited<ReturnType<typeof rpc.vault.list>>["entries"][number];

const ROW = "flex min-h-[52px] items-center justify-between gap-4 px-4 py-2";

/** The logins the Muse may use to sign in for the person: where, as whom, and when. Never a
 * value, so all the person can do here is delete. */
export function VaultSettings({ botId }: { botId: string }) {
  const { t, i18n } = useLingui();
  const [entries, setEntries] = useState<VaultEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void rpc.vault
      .list({ botId })
      .then((next) => {
        if (!cancelled) setEntries(next.entries);
      })
      .catch(() => {
        if (!cancelled) setEntries(null);
      });
    return () => {
      cancelled = true;
    };
  }, [botId]);

  if (!entries?.length) return null;

  async function remove(id: string) {
    setError(null);
    try {
      await rpc.vault.remove({ botId, id });
      setEntries((current) => current?.filter((entry) => entry.id !== id) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t`Could not delete`);
    }
  }

  const day = (iso: string) => i18n.date(new Date(iso), { dateStyle: "medium" });

  return (
    <section data-testid="vault-settings">
      <h3 className="px-4 pb-2 text-[13.5px] text-muted-foreground">
        <Trans>Vault</Trans>
      </h3>
      <div className={MUSE_INSET_GROUP}>
        {entries.map((entry, index) => (
          <div key={entry.id}>
            {index > 0 ? <div className="ms-4 border-t border-border/70" /> : null}
            <div className={ROW}>
              <span className="min-w-0">
                <span className="block truncate text-[15px] text-foreground" dir="auto">
                  {hostOf(entry.site)}
                  {entry.username ? ` · ${entry.username}` : ""}
                </span>
                <span className="block text-[12.5px] text-muted-foreground">
                  {entry.lastUsedAt
                    ? t`Saved ${day(entry.createdAt)} · used ${day(entry.lastUsedAt)}`
                    : t`Saved ${day(entry.createdAt)} · not used yet`}
                </span>
              </span>
              <Button variant="ghost" onClick={() => void remove(entry.id)}>
                <Trans>Delete</Trans>
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
