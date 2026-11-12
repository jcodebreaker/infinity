import { useLingui } from "@lingui/react/macro";
import {
  Checkbox,
  cn,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Input,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@superchat/ui-web";
import {
  HelpCircle,
  MoreHorizontal,
  Pencil,
  Quote,
  Search,
  Sparkles,
  Target,
  Trash2,
  UserRound,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { SuperchatTile } from "../../components/chrome/SuperchatTile";
import { MemoryEmptyLine, MemorySection } from "../../components/ui/MemorySection";
import {
  commitmentLabel,
  isOpenQuestion,
  memoryLabel,
  type PersonMemory,
  parseDue,
  parsePerson,
  questionLabel,
} from "./memoryProfile";

export type MemoryClaim = {
  id: string;
  kind: string;
  text: string;
  origin: "said" | "edited" | "noticed";
  personAuthored: boolean;
  date: number;
  /** The person's own words this came from, when the engine kept them. */
  quote?: string;
  /** Past its end date: Superchat no longer uses it; it stays listed so the person can forget it. */
  expired?: boolean;
};

/** The Memory tab's claim wire: `rpc.memory.claims` / `editClaim` / `forgetClaim`. */
export type ClaimsWire = {
  claims: (input: { botId: string }) => Promise<{ claims: MemoryClaim[] }>;
  editClaim: (input: { botId: string; claimId: string; text: string }) => Promise<MemoryClaim>;
  forgetClaim: (input: { botId: string; claimId: string }) => Promise<{ ok: true }>;
};

/** Which claim kinds each section shows, in reading order. */
export const MEMORY_SECTIONS = [
  { id: "about", kinds: ["fact", "preference", "instruction"] },
  { id: "commitments", kinds: ["commitment"] },
  { id: "projects", kinds: ["project", "decision"] },
  { id: "people", kinds: ["person"] },
  { id: "working", kinds: ["working_style"] },
] as const;

export type SectionId = (typeof MEMORY_SECTIONS)[number]["id"] | "questions";

/** Reading order of every group; open questions sit right after Projects & focus. */
const GROUP_ORDER: SectionId[] = [
  "about",
  "commitments",
  "projects",
  "questions",
  "people",
  "working",
];

/** Open questions (a project claim that is a question, or "needs to resolve …") get their own
 * group, shown right after Projects & focus. Empty groups are dropped. */
export function groupClaims(
  claims: MemoryClaim[],
): Array<{ id: SectionId; claims: MemoryClaim[] }> {
  const groups: Array<{ id: SectionId; claims: MemoryClaim[] }> = [];
  for (const section of MEMORY_SECTIONS) {
    const inSection = claims.filter((claim) =>
      (section.kinds as readonly string[]).includes(claim.kind),
    );
    if (section.id === "projects") {
      groups.push({ id: "projects", claims: inSection.filter((c) => !isOpenQuestion(c.text)) });
      groups.push({ id: "questions", claims: inSection.filter((c) => isOpenQuestion(c.text)) });
    } else {
      groups.push({ id: section.id, claims: inSection });
    }
  }
  return groups.filter((group) => group.claims.length > 0);
}

const UNDO_MS = 4000;

const initials = (name: string) =>
  name
    .split(" ")
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word.charAt(0).toUpperCase())
    .join("");

function shapeOf(sectionId: SectionId, text: string) {
  if (sectionId === "commitments") {
    const { text: rest, due } = parseDue(text);
    return { title: commitmentLabel(rest), due, person: null as PersonMemory | null };
  }
  if (sectionId === "questions") {
    return { title: questionLabel(text), due: null, person: null as PersonMemory | null };
  }
  if (sectionId === "people") {
    const person = parsePerson(text);
    return { title: person.name, due: null, person };
  }
  return { title: memoryLabel(text), due: null, person: null as PersonMemory | null };
}

const iconButton =
  "inline-flex size-7 items-center justify-center rounded-full text-ink-3 outline-none hover:bg-selection hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 data-popup-open:bg-selection data-popup-open:text-foreground";

/** A memory's tile by section: you (blue), projects (green), open questions (purple), how
 * Superchat works with you (indigo). */
function SectionTile({ sectionId }: { sectionId: SectionId }) {
  const [tone, Icon] =
    sectionId === "projects"
      ? (["green", Target] as const)
      : sectionId === "questions"
        ? (["purple", HelpCircle] as const)
        : sectionId === "working"
          ? (["indigo", Sparkles] as const)
          : (["blue", UserRound] as const);
  return (
    <SuperchatTile tone={tone} size={28}>
      <Icon strokeWidth={2.4} />
    </SuperchatTile>
  );
}

function Claim({
  botId,
  sectionId,
  claim,
  wire,
  onChanged,
  onRemoved,
  editing = false,
}: {
  botId: string;
  sectionId: SectionId;
  claim: MemoryClaim;
  wire: ClaimsWire;
  /** The section's Edit is on: the row's actions stay visible. */
  editing?: boolean;
  onChanged: (claim: MemoryClaim) => void;
  onRemoved: (id: string) => void;
}) {
  const { t } = useLingui();
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // Ticking a commitment or forgetting any memory waits a few seconds so it can be undone.
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const latest = useRef({ onRemoved, claimId: claim.id });
  latest.current = { onRemoved, claimId: claim.id };

  const commitForget = async () => {
    window.clearTimeout(timer.current);
    pendingRef.current = false;
    try {
      await wire.forgetClaim({ botId, claimId: latest.current.claimId });
      latest.current.onRemoved(latest.current.claimId);
    } catch {
      setPending(false);
      setFailed(true);
    }
  };
  const commitForgetRef = useRef(commitForget);
  commitForgetRef.current = commitForget;

  useEffect(
    () => () => {
      // Leaving the tab (or a search hiding the row) never drops a pending forget.
      if (pendingRef.current) void commitForgetRef.current();
    },
    [],
  );

  const startForget = () => {
    setFailed(false);
    setPending(true);
    pendingRef.current = true;
    timer.current = window.setTimeout(() => void commitForgetRef.current(), UNDO_MS);
  };
  const undo = () => {
    window.clearTimeout(timer.current);
    pendingRef.current = false;
    setPending(false);
  };

  const save = async () => {
    if (draft === null || draft.trim() === "" || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      onChanged(await wire.editClaim({ botId, claimId: claim.id, text: draft.trim() }));
      setDraft(null);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const when = new Date(claim.date * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  const source =
    claim.origin === "edited"
      ? t`Edited by you`
      : claim.origin === "said"
        ? t`You said, ${when}`
        : t`Noticed, ${when}`;
  const { title, due, person } = shapeOf(sectionId, claim.text);
  const isCommitment = sectionId === "commitments";

  if (draft !== null) {
    return (
      <li className="superchat-row superchat-row-plain px-3 py-2" data-testid="memory-claim">
        <textarea
          // biome-ignore lint/a11y/noAutofocus: the person just chose to edit this memory
          autoFocus
          rows={2}
          aria-label={t`Edit memory`}
          className="w-full resize-none rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-[14px] leading-5 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          dir="auto"
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void save();
            } else if (event.key === "Escape") {
              event.stopPropagation();
              setDraft(null);
            }
          }}
        />
        <p className="mt-1 text-[12px] leading-4 text-muted-foreground">
          {failed ? (
            <span className="text-destructive">{t`Could not save`}</span>
          ) : (
            t`Enter to save · Esc to cancel`
          )}
        </p>
      </li>
    );
  }

  if (pending) {
    return (
      <li
        className="superchat-row flex min-h-[52px] items-center gap-3 px-3 py-2 text-[14px] leading-5"
        data-testid="memory-claim-pending"
      >
        {isCommitment ? <Checkbox checked className="mt-0.5" aria-label={title} /> : null}
        <span className="min-w-0 flex-1 truncate text-muted-foreground line-through">{title}</span>
        <button
          type="button"
          className="shrink-0 text-[13px] leading-5 text-link underline-offset-2 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
          onClick={undo}
        >
          {t`Undo`}
        </button>
      </li>
    );
  }

  return (
    <li
      className="group/claim superchat-row flex min-h-[52px] items-center gap-3 px-3 py-2 transition-colors hover:bg-selection focus-within:bg-selection"
      data-testid="memory-claim"
    >
      {isCommitment && !claim.expired ? (
        <span className="grid size-7 shrink-0 place-items-center">
          <Checkbox
            checked={false}
            aria-label={t`Mark done: ${title}`}
            onCheckedChange={startForget}
          />
        </span>
      ) : person ? (
        <SuperchatTile tone="orange" size={28}>
          <span className="text-[11px] font-semibold">{initials(person.name)}</span>
        </SuperchatTile>
      ) : (
        <SectionTile sectionId={sectionId} />
      )}
      <div className="min-w-0 flex-1">
        {person ? (
          <>
            <p className="truncate text-[14px] leading-5 text-foreground" dir="auto">
              {person.name}
            </p>
            {person.relation || person.facts ? (
              <p className="truncate text-[12px] leading-4 text-ink-3" dir="auto">
                {[person.relation, person.facts].filter(Boolean).join(" · ")}
              </p>
            ) : null}
          </>
        ) : (
          <p
            className={cn(
              "line-clamp-2 text-[14px] leading-5",
              claim.expired ? "text-ink-3" : "text-foreground",
            )}
            dir="auto"
          >
            {title}
          </p>
        )}
        {claim.expired ? (
          <span className="mt-0.5 inline-flex h-5 items-center rounded-md bg-muted px-1.5 text-[11px] leading-none text-muted-foreground">
            {t`Expired`}
          </span>
        ) : due ? (
          <span
            className={cn(
              "mt-0.5 inline-flex h-5 items-center rounded-md px-1.5 text-[11px] leading-none",
              due.overdue ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground",
            )}
          >
            {due.label}
          </span>
        ) : null}
        {failed ? (
          <p className="text-[12px] leading-4 text-destructive">{t`Could not save`}</p>
        ) : null}
      </div>
      <span
        className={cn(
          "flex shrink-0 items-center gap-0.5 transition-opacity group-focus-within/claim:opacity-100 group-hover/claim:opacity-100 has-data-popup-open:opacity-100 [@media(hover:none)]:opacity-100",
          editing ? "opacity-100" : "opacity-0",
        )}
      >
        <Tooltip>
          <TooltipTrigger aria-label={t`Source`} className={iconButton}>
            <Quote className="size-3.5" aria-hidden />
          </TooltipTrigger>
          <TooltipContent side="left" className="flex-col items-start gap-0.5">
            <span>{source}</span>
            {claim.quote ? <span className="opacity-70">“{claim.quote}”</span> : null}
          </TooltipContent>
        </Tooltip>
        <DropdownMenu>
          <DropdownMenuTrigger aria-label={t`More`} className={iconButton}>
            <MoreHorizontal className="size-3.5" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-auto min-w-32">
            {claim.expired ? null : (
              <DropdownMenuItem onClick={() => setDraft(claim.text)}>
                <Pencil aria-hidden />
                {t`Edit`}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem variant="destructive" onClick={startForget}>
              <Trash2 aria-hidden />
              {t`Forget`}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </span>
    </li>
  );
}

/** The Memory tab's editable sections: About you, Commitments, Projects & focus, Open
 * questions, People, How Superchat works with you. Items show a short label (the stored text is
 * untouched); source and quote sit behind a hover icon; an edit marks an item person-authored
 * (the engine's upkeep and Helpers never overwrite it); forgetting waits for an undo. */
export function MemorySections({
  botId,
  wire,
  claims,
  onChange,
}: {
  botId: string;
  wire: ClaimsWire;
  claims: MemoryClaim[];
  onChange: (next: MemoryClaim[]) => void;
}) {
  const { t } = useLingui();
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<SectionId | null>(null);
  const claimsRef = useRef(claims);
  claimsRef.current = claims;

  const titles: Record<SectionId, string> = {
    about: t`About you`,
    commitments: t`Commitments`,
    projects: t`Projects & focus`,
    questions: t`Open questions`,
    people: t`People`,
    working: t`How superchat works with you`,
  };

  const needle = query.trim().toLowerCase();
  const matches = (claim: MemoryClaim) =>
    needle === "" || `${claim.text} ${memoryLabel(claim.text)}`.toLowerCase().includes(needle);
  const groups = groupClaims(claims);
  const byId = new Map(groups.map((group) => [group.id, group.claims]));
  const visible = GROUP_ORDER.map((id) => ({
    id,
    claims: (byId.get(id) ?? []).filter(matches),
    total: (byId.get(id) ?? []).length,
  })).filter((group) =>
    needle === "" ? group.id !== "questions" || group.total > 0 : group.claims.length > 0,
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="relative">
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-3"
          aria-hidden
        />
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t`Find a memory`}
          aria-label={t`Find a memory`}
          className="h-8 rounded-lg border-0 bg-selection pl-8 text-[13px] md:text-[13px]"
        />
      </div>
      {visible.length === 0 ? <MemoryEmptyLine>{t`No matches`}</MemoryEmptyLine> : null}
      {visible.map((group) => (
        <MemorySection
          key={group.id}
          id={group.id}
          title={titles[group.id]}
          count={group.claims.length}
          forceOpen={needle !== ""}
          editing={editing === group.id}
          onEdit={
            group.claims.length > 0
              ? () => setEditing((current) => (current === group.id ? null : group.id))
              : undefined
          }
        >
          {group.claims.length === 0 ? (
            <MemoryEmptyLine>{t`Nothing yet`}</MemoryEmptyLine>
          ) : (
            <ul className="superchat-group flex flex-col">
              {group.claims.map((claim) => (
                <Claim
                  key={claim.id}
                  botId={botId}
                  sectionId={group.id}
                  claim={claim}
                  wire={wire}
                  editing={editing === group.id}
                  onChanged={(saved) =>
                    onChange(claimsRef.current.map((c) => (c.id === saved.id ? saved : c)))
                  }
                  onRemoved={(id) => onChange(claimsRef.current.filter((c) => c.id !== id))}
                />
              ))}
            </ul>
          )}
        </MemorySection>
      ))}
    </div>
  );
}
