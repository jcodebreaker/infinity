import { Trans, useLingui } from "@lingui/react/macro";
import type { ReplyCardDataOf } from "@superchat/contracts";
import { Button, Input } from "@superchat/ui-web";
import { Check } from "lucide-react";
import { useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";
import { Frame } from "./catalog";
import { useReplyCardBotId } from "./context";
import { hostOf } from "./links";

/**
 * The Muse asked for a login. The value goes from these fields straight to the vault through
 * Superchat's API: it is never a chat message, and the field is emptied the moment it is submitted.
 */
export function SecureEntryCard({ data }: { data: ReplyCardDataOf<"secure_entry"> }) {
  const { t } = useLingui();
  const botId = useReplyCardBotId();
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const host = hostOf(data.site);

  // A card already answered (this session or an earlier one) opens as "Saved".
  useEffect(() => {
    if (!botId) return;
    let cancelled = false;
    void rpc.vault
      .request({ botId, requestId: data.requestId })
      .then((request) => {
        if (!cancelled && request.status === "saved") setSaved(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [botId, data.requestId]);

  async function submit() {
    if (!botId || !password) return;
    const value = { username: username.trim(), password };
    setBusy(true);
    setError(null);
    setPassword("");
    try {
      await rpc.vault.save({
        botId,
        requestId: data.requestId,
        name: data.name,
        site: data.site,
        ...(value.username ? { username: value.username } : {}),
        password: value.password,
      });
      setUsername("");
      setSaved(true);
    } catch {
      setError(t`Could not save. Try again.`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Frame title={t`Sign-in for ${host}`}>
      {saved ? (
        <p
          data-testid="secure-entry-saved"
          className="flex items-center gap-1.5 text-[14px] text-muted-foreground"
        >
          <Check size={14} className="text-success" />
          <Trans>Saved</Trans>
        </p>
      ) : (
        <form
          data-testid="secure-entry-form"
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {data.reason ? (
            <p className="text-[13px] text-muted-foreground" dir="auto">
              {data.reason}
            </p>
          ) : null}
          <Input
            aria-label={t`Username (optional)`}
            placeholder={t`Username (optional)`}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
          <Input
            aria-label={t`Password`}
            placeholder={t`Password`}
            type="password"
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <p className="text-[12px] text-muted-foreground">
            <Trans>Goes to your vault, not the chat.</Trans>
          </p>
          <Button type="submit" className="self-start" disabled={!botId || !password || busy}>
            {busy ? <Trans>Saving…</Trans> : <Trans>Save</Trans>}
          </Button>
          {error ? (
            <p role="alert" className="text-[13px] text-destructive">
              {error}
            </p>
          ) : null}
        </form>
      )}
    </Frame>
  );
}
