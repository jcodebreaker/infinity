import { useLingui } from "@lingui/react/macro";
import {
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Check, ChevronDown, Download, Loader2, Palette, Presentation, X } from "lucide-react";
import { downloadArtifactBytes } from "../../lib/artifact-open";
import type { DeckExportFormat, ExportState } from "./DeckExport";
import { THEME_BUTTON_ATTR } from "./DeckThemePicker";
import { requestDeckPresent, setDeckThemeOpen, useDeckThemeOpen } from "./deck-ui-state";

/** A deck's actions in the editor header, after the shell's Edit: Theme, Present and one
 * Download menu. Progress, failure and the "added to your Library" note all show on the
 * Download button itself. */
export function DeckHeaderActions({
  deckKey,
  state,
  onExport,
  onCancel,
  source,
}: {
  /** The deck's file name: the key the Theme gallery and present requests are stored under. */
  deckKey: string;
  state: ExportState;
  onExport: (format: DeckExportFormat) => void;
  onCancel: () => void;
  /** The deck's own HTML, for "HTML (source)". */
  source: { name: string; mimeType: string; bytes: Uint8Array } | undefined;
}) {
  const { t } = useLingui();
  const themeOpen = useDeckThemeOpen(deckKey);
  const working = state.kind === "working";

  const label =
    state.kind === "working"
      ? state.format === "pptx"
        ? t`Preparing PowerPoint…`
        : t`Preparing PDF…`
      : state.kind === "done"
        ? t`Added to your Library`
        : state.kind === "error"
          ? state.format === "pptx"
            ? t`Couldn't prepare PowerPoint`
            : t`Couldn't prepare PDF`
          : t`Download`;

  return (
    <>
      <Button
        variant={themeOpen ? "default" : "secondary"}
        size="sm"
        aria-pressed={themeOpen}
        aria-haspopup="dialog"
        {...{ [THEME_BUTTON_ATTR]: "" }}
        onClick={() => setDeckThemeOpen(deckKey, !themeOpen)}
      >
        <Palette size={13} />
        {t`Theme`}
      </Button>
      <Button variant="secondary" size="sm" onClick={() => requestDeckPresent(deckKey)}>
        <Presentation size={13} />
        {t`Present`}
      </Button>
      {state.kind === "error" ? (
        <Button
          variant="secondary"
          size="sm"
          data-testid="deck-download"
          onClick={() => onExport(state.format)}
        >
          <span className="text-muted-foreground">{label}</span>
          <span>{t`Try again`}</span>
        </Button>
      ) : working ? (
        <span className="inline-flex items-center gap-1">
          <Button
            variant="secondary"
            size="sm"
            data-testid="deck-download"
            disabled
            aria-busy="true"
          >
            <Loader2 className="animate-spin motion-reduce:animate-none" />
            {label}
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label={t`Cancel`} onClick={onCancel}>
            <X />
          </Button>
        </span>
      ) : (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="secondary" size="sm" data-testid="deck-download" />}
          >
            {state.kind === "done" ? <Check size={13} /> : <Download size={13} />}
            <span role={state.kind === "done" ? "status" : undefined}>{label}</span>
            <ChevronDown size={12} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              onClick={() => onExport("pptx")}
            >{t`PowerPoint (.pptx)`}</DropdownMenuItem>
            <DropdownMenuItem onClick={() => onExport("pdf")}>{t`PDF`}</DropdownMenuItem>
            <DropdownMenuItem
              disabled={!source}
              onClick={() =>
                source && downloadArtifactBytes(source.name, source.mimeType, source.bytes)
              }
            >
              {t`HTML (source)`}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </>
  );
}
