import { useLingui } from "@lingui/react/macro";
import type { ArtifactVersion } from "@superchat/contracts";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Check, ChevronDown, History } from "lucide-react";
import { Fragment, type ReactNode, useState } from "react";
import { formatRelativeTime } from "../../../lib/relative-time";
import { useShowMessage } from "../../../lib/show-message";
import { isHandOrigin } from "./editor-state";

/**
 * "<Kind> · version N ▾" beside the title: every version with who made it and when; picking one
 * shows it, and "Restore this version" makes the one on screen the newest again (a new version,
 * nothing is lost).
 */
export function VersionMenu({
  kindLabel,
  version,
  versions,
  shownId,
  onPick,
  onRestore,
  note,
  originOf,
}: {
  kindLabel: string;
  /** The version on screen, for before (or without) the version list. */
  version: number;
  /** Newest first. */
  versions: readonly ArtifactVersion[];
  shownId: string | null;
  onPick: (versionId: string) => void;
  /** Restores the version on screen; resolves when the new version is saved. */
  onRestore: () => Promise<void>;
  /** A quiet line at the bottom of the menu, from an extension. */
  note?: ReactNode;
  /** Words each version's origin (the file's studio names five, ADR 0024). */
  originOf?: (entry: ArtifactVersion) => string;
}) {
  const { t } = useLingui();
  const showMessage = useShowMessage();
  const [restoring, setRestoring] = useState(false);
  const [failed, setFailed] = useState(false);
  const shown = versions.find((entry) => entry.id === shownId) ?? versions[0];
  const label = t`${kindLabel} · version ${shown?.version ?? version}`;
  // A version saved in the file's studio came from a message there, not in the Conversation.
  const conversationMessage = (entry: ArtifactVersion) =>
    entry.fromStudio ? null : (entry.itemId ?? null);
  // One version opens the menu only to reach the message it came from.
  const first = versions[0];
  if (versions.length < 2 && !note && !(first && conversationMessage(first) && showMessage)) {
    return (
      <span data-testid="version-label" className="shrink-0 text-[12px] text-muted-foreground">
        {label}
      </span>
    );
  }
  const latest = versions[0];
  const older = !!shown && !!latest && shown.id !== latest.id;
  const origin = (entry: ArtifactVersion) =>
    originOf
      ? originOf(entry)
      : conversationMessage(entry)
        ? t`from your message`
        : entry.origin === "update"
          ? t`updated`
          : !isHandOrigin(entry.origin)
            ? t`by superchat`
            : entry.origin === "restore"
              ? t`restored`
              : t`by you`;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        data-testid="version-menu"
        className="inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-1.5 text-[12px] text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        {label}
        <ChevronDown size={12} aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t`Versions`}</DropdownMenuLabel>
          {versions.map((entry, index) => {
            const originLine = [origin(entry), formatRelativeTime(entry.createdAt)]
              .filter(Boolean)
              .join(" · ");
            // The message it came from is its own row, so choosing it scrolls the conversation
            // instead of picking the version.
            const messageId = showMessage ? conversationMessage(entry) : null;
            return (
              <Fragment key={entry.id}>
                <DropdownMenuItem
                  data-testid="version-item"
                  onClick={() => onPick(entry.id)}
                  className="gap-2"
                >
                  <Check
                    size={14}
                    aria-hidden="true"
                    className={entry.id === shown?.id ? "" : "invisible"}
                  />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="text-[13px]">
                      {index === 0
                        ? t`Version ${entry.version} · latest`
                        : t`Version ${entry.version}`}
                    </span>
                    {messageId ? null : (
                      <span className="truncate text-[11.5px] text-muted-foreground">
                        {originLine}
                      </span>
                    )}
                  </span>
                </DropdownMenuItem>
                {messageId ? (
                  <DropdownMenuItem
                    data-testid="version-message"
                    onClick={() => showMessage?.(messageId)}
                    className="-mt-1 py-0.5 ps-8 text-[11.5px] text-muted-foreground"
                  >
                    <span className="truncate">{originLine}</span>
                  </DropdownMenuItem>
                ) : null}
              </Fragment>
            );
          })}
        </DropdownMenuGroup>
        {older ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid="version-restore"
              disabled={restoring}
              onClick={async () => {
                setRestoring(true);
                setFailed(false);
                try {
                  await onRestore();
                } catch {
                  setFailed(true);
                } finally {
                  setRestoring(false);
                }
              }}
            >
              <History size={14} aria-hidden="true" />
              {failed ? t`Couldn't restore. Try again` : t`Restore this version`}
            </DropdownMenuItem>
          </>
        ) : null}
        {note ? (
          <>
            <DropdownMenuSeparator />
            <p
              data-testid="version-note"
              className="px-2 py-1.5 text-[11.5px] text-muted-foreground"
            >
              {note}
            </p>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
