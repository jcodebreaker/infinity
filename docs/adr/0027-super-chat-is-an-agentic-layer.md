# Super Chat is an agentic layer

> **Status:** Accepted 2026-10-17. Amends [ADR 0007](0007-the-muse-delegates-multi-step-work.md) (deck tools count toward the step limit), [ADR 0022](0022-the-orchestrator-narrates-while-helpers-work.md) (narration), [ADR 0023](0023-the-plan-is-visible-and-the-work-is-alive.md) (the engine ticks the plan; the panel shows it), [ADR 0025](0025-act-by-default-tasks-have-state.md) (model categories, evidence) and [ADR 0026](0026-a-turn-speaks-in-a-second.md) (no cheap opener). Amended 2026-10-25: the session panel is removed; see [Amendments](#amendments).

After the latency work a deck request finished in 33 seconds and still felt like one call to a cheap model. It was: the chat ran the connection's default model while Settings named a stronger one; the assistant built the deck inside its own turn, so nothing could narrate, answer or show drafts; it never looked at what the person had made before; the plan was the model's word, ticked by hand; and the display text was half written by the web. The loop belongs to the harness. The behaviour, the system around the loop and what the person sees belong to us, and they were thin.

## Decision

**Three model categories, never names.** A person has three picks per harness: **Foundation** (the most capable: context, plan, delegation, judgement), **Builder** (helpers that write decks, pages, documents, sheets) and **Light** (titles, summaries, rerank, fetch-and-compare helpers). The picks are chosen from the catalog by capability tier and price, each overridable in Settings. The Super Chat runs on Foundation. A helper runs on the category the assistant names (`light`, `builder`, `foundation`; Builder by default; Foundation only for judgement). Settings is the source of truth: a change re-resolves the Super Chat's model, "Default" in the header stores the person's Foundation pick, never the connection's default, and the header shows the model in use. No model name appears in code or prompts.

**The turn, in order.** The engine stamps and stores the message; the screen lights at once. Foundation speaks one sentence first. Its first tool call is `context_brief`: the engine's own account of what the person has made before on the subject, what earlier turns and the journal say, standing preferences, open tasks and matching sources, built by code plus one Light planner call, started at accept so the call returns at once. Foundation renders the plan once: steps in the person's words, each with a present-tense form, an owner and a done criterion that names the evidence; step one is the brief, the last is a check. Deliverables go to a Builder helper whose brief carries the facts, the look, the sources and the done criterion; it reports milestones, saves drafts, flags judgement calls, and runs the check as its last act. The engine ticks the steps from evidence it holds. Foundation reviews the check output and the file's outline against the ask before handing back; a failure adds a fix step and messages the helper, at most twice. The hand-back is under eighty words with at most three things to look at. The decision is written once into the task record, the working list and the journal.

**A step is done only with evidence.** The model never ticks a step. A helper started for a step makes it doing; its result makes it done; a check tool that ran after the build makes the check step done; a save lists the output. A check step with no action behind it reads "Not checked", and the hand-back must say so. This is where the pattern we copied was hollow, and where this record departs from it.

**The assistant speaks three times.** The opening sentence, a decision or a surprise, and the hand-back. Progress is the panel's job, not prose.

**Display is the engine's.** Step titles, the summary line under a reply ("Created 2 files, searched the web, and 12 more steps"), the fold of earlier steps and the step states are written by the engine's projection from the plan and the activity steps. The web renders them in two views: the work strip in the transcript (compact) and a session panel beside it while work runs (Progress, Outputs, Used in this conversation), with a comment on any step that travels with the next message. Pause holds a reply's helpers at their next tool boundary and resumes them; unresumed for thirty minutes, they are cancelled with one line in the chat.

## Consequences

- Engine: the three picks and their resolution; `context_brief` and the recall hint on by default; plan items with `active`, owner and `done_when`; the task record holds the plan and its evidence; `deck_` tools count toward the step limit; an `artifact_outline` read for the review; the decision line into the working list and journal; the summary line and fold in the activity projection; pause and resume routes and the runner's hold at a tool boundary.
- Prompt: "How a turn runs", in our words: first sentence, brief, plan once, delegate, never tick, speak three times, check last, review, finish short; recall before the format rules.
- Web: the model in use in the header and three picks in Settings; the strip's active form, fold, "Not checked", "Stopped", "Paused"; the summary line; the session panel; the step comment chip.
- Glossary: Foundation, Builder, Light, Context brief, Done criterion, Not checked, Session panel, Paused, Comment.

## Considered options

- **A cheap model as an opener while the strong one plans.** Rejected: two voices in one reply, and the sub-second word it buys is an acknowledgement the strip already gives; Foundation's first token at two to three seconds is honest.
- **Let the model tick its own steps, as the pattern we studied does.** Rejected: the trace showed a check step ticked with nothing behind it; evidence the engine holds is the only tick that means something.
- **Item-level plan tools instead of the living card.** Not now: the engine's ticks remove most model updates; revisit if the model keeps rewriting the list.
- **Keep the deck inline for speed.** Rejected: a few seconds of helper start buys narration, side answers, drafts, flags and a review.

## Amendments

**Amended 2026-10-25: the session panel is removed.**

- It opened on its own beside the transcript when a reply ran. With Progress gone it showed only Outputs, a second copy of Files, and "Used in this conversation". While open it hid the Activity button.
- Outputs live in Files. Steps live in Agent Activity and the work strip. There is no panel and no header toggle (`features/session-panel/` is deleted).
- The Activity button shows on desktop whenever a Conversation is open.
- The engine still sends `families` on each Activity; nothing in the web reads it now.
