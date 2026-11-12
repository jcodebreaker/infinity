import type { Ask } from "@superchat/contracts";
import type { TileTone } from "../../components/chrome/SuperchatTile";

type Choice = Ask["choices"][number];

/** An open Ask as the inspector's decision card (the old design notes "Waiting on you"). */
export type AskDecision = {
  tone: TileTone;
  title: string;
  subtitle?: string;
  /** Two equal buttons when the Ask is a plain pick: the secondary on the left, the primary
   * (the Ask's first choice, as everywhere else) on the right. Null when it needs typing or
   * has more choices than fit: the card then opens Waiting on you. */
  actions: { primary: Choice; secondary: Choice | null } | null;
};

const KIND_TONE: Record<Ask["kind"], TileTone> = {
  approval: "orange",
  question: "blue",
  blocked_task: "yellow",
  proposal: "purple",
  skill_offer: "indigo",
};

/** Maps an Ask onto the decision card, with the same title and subtitle rules as `AskItem`. */
export function askDecision(ask: Ask, approvalTitle: string): AskDecision {
  const engineApproval = ask.approval !== undefined;
  const title = engineApproval ? ask.text : ask.kind === "approval" ? approvalTitle : ask.text;
  const subtitle = engineApproval
    ? ask.detail
    : ask.kind === "approval"
      ? ask.text
      : ask.kind === "proposal" || ask.kind === "skill_offer"
        ? ask.detail
        : undefined;
  const [primary, secondary, ...rest] = ask.choices;
  const actions =
    ask.input || !primary || rest.length > 0 ? null : { primary, secondary: secondary ?? null };
  return { tone: KIND_TONE[ask.kind], title, subtitle: subtitle || undefined, actions };
}
