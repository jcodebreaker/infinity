# The orchestrator narrates while helpers work

> **Status:** Done 2026-10-17: `report_progress` and `raise_flag` in the engine, the narration rules in the assistant bundle, the work strip in the web. Contract: [`docs/conversation.md`](../conversation.md). First-token and first-progress timings are logged, not yet measured in use.

The assistant delegates multi-step work to helpers ([ADR 0007](0007-the-muse-delegates-multi-step-work.md)). Today a delegation goes quiet: the helper runs, the parent waits for a completion notice, and the person sees a working indicator and an activity list of tool titles. For someone who needs an answer on time, that silence reads as "nothing is happening", and the activity list reads as machinery. Time to first token and time to the first sentence of progress are what people feel.

The assistant stays in the conversation. It speaks first, reports progress in sentences, surfaces judgement calls as soon as it makes them, and hands back short.

## Decision

- **Speak first, then delegate.** The reply to a request starts with one paragraph that says what the assistant will do, streamed from the strong model. Helpers are started after that paragraph, never before it. No file reads or lookups happen before the first sentence.
- **Progress is a message.** A helper posts a one-line progress notice to its parent at each step it completes (a file read, a comparison, N slides written), throttled to one per step. The parent narrates it in one or two sentences in its running reply. The notice is a runtime message, like the completion notice today, and the helper's transcript never enters the parent's context.
- **Flags wake the parent.** When a helper chooses between sources, deviates from the ask, or finds something the person should know before the end, it raises a flag. A flag always wakes the parent, which tells the person right away in its own words.
- **Narrate decisions, not mechanics.** The prompt rules: say what you decided ("I'm using the ledger"), not what you ran; say surprises as soon as you know them; on hand-back name at most three things to look at, and put the rest in the artifact; never let a spinner stand without a sentence next to it.
- **The activity panel is a view.** It shows the activity projection in plain words, as it does today. "Technical details" is the same log unfolded: commands, files, model and tokens. No second status feed enters the chat.
- **Questions are answered on the main turn.** A new message while helpers run is answered now, from the strong model, and the helpers keep going. The reply notes that the work continues.
- **Drafts are versions.** A helper writing an artifact saves draft versions as it goes, so the work strip under the reply can show the artifact filling in, and the editor can show "updated by superchat" when the person has it open.

## Consequences

- Engine: a `progress` notice and a `flag` notice alongside the completion notice; the parent's wake handling narrates instead of only reading the inbox; a throttle per helper step. Prompt: the rules above in the assistant bundle and the helper types.
- Web: the work strip under a reply (label, elapsed time, Show details, an artifact strip when a draft exists) and helper cards attached to the message that started them. The activity panel is unchanged.
- Budget, measured: under one second to the first word, one progress sentence per helper step, hand-back under 80 words plus the artifact card. Time to first token and time to the first progress sentence are logged separately.
- The pattern matches what other orchestrators converged on in 2026: the coordinator's narration on its own stream, each worker a first-class entity with status, worker transcripts kept out of the coordinator's context.

## Considered options

- **Stream helper tokens into the chat.** Rejected: that is the machinery the person should not have to read, and it floods the parent's context.
- **Narrate only at completion.** Rejected: that is today's behaviour, and the silence is the problem.
- **A separate progress feed in the chat, apart from the reply.** Rejected: two voices in one conversation; the assistant's reply is the one place to say what is happening.

## Update: progress wakes an idle parent (review, 2026-10-17)

The first build steered a progress notice only into a turn streaming at that moment. The parent's turn ends right after it starts its helpers, so in the usual one-helper case no progress line ever reached the chat; it was held and then dropped when that helper finished. "Narrate only at completion", the rejected option above, is what shipped.

- A progress notice still tries the running turn first; when the parent is idle it **wakes it**, at most once every 30 seconds per parent, with every helper's newest waiting line in one notice. A flag's wake and a completion notice carry the waiting lines the same way and restart the interval. Each wake is a short model turn; that is the price of narration, bounded by the interval.
- Helpers report at most one line per minute of work, and **save a file early and again as it fills in**, so "drafts are versions" is a prompt rule, not a hope.
- The first-token log line counts from the runner's turn start, after the engine's dispatch and any host wake: it measures the model and the prefix, not the person's wait.
- The runner's notice state does not survive its restart.

Contract: [`docs/conversation.md`](../conversation.md#helper-notices).

## Update: the assistant speaks three times (ADR 0027, 2026-10-17)

Progress is the panel's job. The assistant speaks at its first sentence, at a decision or a surprise, and at the hand-back; helper notices move the plan, never prose. The sentence-per-progress rule above is withdrawn.
