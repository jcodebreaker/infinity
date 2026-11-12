# The conversation

There is one conversation per person. The assistant talks in it, helpers work under it, and artifacts come out of it. This page is the contract for how a message links to an artifact and how versions link back to messages ([ADR 0021](adr/0021-one-conversation-messages-link-to-artifacts.md)), for how the plan is ticked and shown ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md), reversed by [ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)), and for how the assistant narrates while helpers work ([ADR 0022](adr/0022-the-orchestrator-narrates-while-helpers-work.md)). It replaces the side-chat and fork sections of the earlier docs. Edits made with a file open in its studio go to the file's own chat, not here ([ADR 0024](adr/0024-the-artifact-studio.md)); the studio's contract is in that record, and the points where it changes this page are marked.

## Words

- **Conversation**: the one chat. Its transcript is a list of items: the person's messages, the assistant's replies, cards and notices.
- **Helper**: a child session the assistant starts for a bounded job. It has its own transcript, read only through the activity panel; nothing from it enters the conversation except the assistant's own words about it.
- **Artifact**: a file the assistant saved into the conversation (a deck, a page, a document, a mini app). It has versions.
- **Chip**: the small pointer on a message that says which artifact, and optionally which part of it, the message is about.

## A message links to an artifact

Two kinds of chip, one look.

**Hard scope.** The person selects elements in the canvas editor. The composer carries the editor's chip (`Slide 4 · growth-title`) and the message travels with a `<superchat-element-request>` block ahead of the words. Only those elements change. This exists today and does not change.

**Soft scope.** The person says what to change with nothing selected. The message still gets a chip, filled in by the engine:

1. **Open in the panel.** When an artifact is open beside the chat, the web sends its id and version with the message (`send.context.openArtifact`). The message shows the chip at once (`Q3 Lending Review`). The assistant is told the artifact is open; it is a hint, not a hard scope. This is the main composer only: the panel's own "Ask superchat to change this file…" box opens the file's studio and sends there ([ADR 0024](adr/0024-the-artifact-studio.md)).
2. **Just made or just edited.** When nothing is open, the engine looks up the artifacts saved or edited in this conversation, newest first, and tells the assistant their names, ids and versions in a short runtime notice at the start of the turn. No model call is spent on this.
3. **By name.** For older artifacts the assistant uses recall and `artifact_list` as it does today.

The chip for cases 2 and 3 appears when the work is done, not when the message is sent: the engine fills it in the moment the turn saves a version of an artifact. That is the honest answer to "which artifact did you mean"; the assistant's first sentence says it in words before that.

**A wrong guess is one tap.** Tapping the chip opens a small list of the conversation's recent artifacts plus "None". Picking one updates the link. If the turn is still running, the engine steers the assistant with one line ("The person means `<name>`"); if the turn is over, the next message carries the corrected link as its hint.

## A version links to a message

Every save is a version: by the assistant from a message (`ai`), by hand in the editor (`manual`), by restore (`restore`), by a live update (`update`). A version saved during a turn records the id of the person's message that started the turn. The version menu in the editor shows it as "from your message · 3 min ago"; choosing it scrolls the conversation to that message. Versions saved by hand have no message and show "edited by hand", as today. A version saved from a file's studio records the studio message that led to it, and the version menu reads one of five origins: from this chat, from Super Chat, by you, restored, updated from source ([ADR 0024](adr/0024-the-artifact-studio.md)).

## Contract

Web ↔ engine, additive. Names are final; shapes may grow. The web half is merged (`a2812bace`..`d3e2933e0`); the engine matches these shapes.

**Message item.** The person's message gains an optional field:

```
artifact: null | {
  artifactId: string,
  name: string,            // file name at the time of linking
  version: number | null,  // the version the message produced, once there is one
  how: "open" | "edited" | "chosen"   // open beside the chat / filled in by a save / picked on the chip
}
```

`how: "open"` is set at send time, `"edited"` by the engine when a save happens during the turn, `"chosen"` when the person taps the chip. A change arrives on the family stream as an item update; the web redraws the chip.

**Send.** `chats.send` gains `context?: { openArtifact?: { artifactId: string, version: number } }`. On the wire the context rides at the top level of the message event, beside `data`, so it never lands in the model's input item:

```
{ "type": "message", "data": { "role": "user", "content": "…" },
  "context": { "open_artifact": { "artifact_id": "art_…", "version": 4 } } }
```

The engine turns it into the runtime hint and the `how: "open"` link on the stored item (`artifact: { artifact_id, name, version, how }` in snake_case on the wire).

**Relink.** `chats.setMessageArtifact({ botId, itemId, artifactId: string | null })` → `POST /v1/sessions/{id}/items/{itemId}/artifact` with body `{ "artifact_id": "art_…" | null }`. The engine updates the link (`how: "chosen"`), steers a running turn, and answers with the updated transcript item in the same shape as a transcript page item. `404 not_found` when the item is not the person's message.

**Versions.** `ArtifactVersion` gains `itemId: string | null`, the person's message that led to it; on the wire `all_versions[].item_id`. The engine sets it on every version saved during a turn the person's message started. The web falls back to `turn_item_id` when `item_id` is absent and the version is the conversation's own non-manual save; that fallback goes once the engine sends `item_id`.

**Runtime hint.** A system notice at the start of a turn, never longer than ten lines:

```
Artifacts in this conversation (newest first):
- Q3 Lending Review (deck, v4, art_…) — open beside the chat
- Provisions memo (document, v1, art_…)
```

The hint is derived from the artifact store and the send context. It is not a tool and not a model call.

**Engine details** (the engine half, `engine/omnigent/superchat/artifacts/`):

- *Where the link lives.* One row per linked message in the artifact store's `message_artifacts` table (session, item, artifact id, name, version, how). A person's message with no row has no `artifact` field; one the person set to "None" has `artifact: null`. Deleting an artifact drops the links to it.
- *Item update.* A link change is sent on the family stream as `message.delivery {chat_id, item_id}`, the frame the web already refetches the transcript on.
- *Send.* The engine reads `context.open_artifact.artifact_id` (32 hex) when it stores the message, before the runner sees it; any other shape, or an artifact of another Conversation, is ignored. The link starts as `how: "open"` with `version: null`.
- *Link on save.* A save during the message's turn sets `how: "edited"` when the message has no link or is `open` with another artifact; a link to the same artifact (`open`, `edited` or `chosen`) gains the saved `version`; a link the person `chosen` for another artifact, or "None", is kept.
- *Versions.* `all_versions[].item_id` is the Conversation's own save's turn message; for a Helper's save (or its own Helper's), the person's message before the `start_helper` call that started the Helper under the Conversation. Hand edits, restores, live updates and scheduled Helpers have `null`. A save from a file's studio session (helper type `editor`, ADR 0024) records the studio turn's own message, and link-on-save in `message_artifacts` is skipped for it.
- *Relink.* `404 not_found` when the item is not the person's message in that session (an assistant item, a runtime notice, an unknown id) or the artifact is not this Conversation's; `400 invalid_input` for a malformed artifact id. When the session's turn is running and the item is its newest person message, the turn is steered through the runner's `if_running` path with exactly one line: `The person means the artifact "<name>" (<artifact_id>).` or `The person says this message is not about an artifact.` An idle session is not steered.
- *Hint.* The runner reads it from `GET /v1/artifacts/hint?session_id=<id>` (`{"hint": string | null}`) at the start of every turn in the Conversation and puts it before the turn's message. At most 8 artifacts (newest version of each, newest first), each `- <title or file name> (<kind>, v<version>, <version id>)`, the kind one of `deck`, `page`, `document`, `PDF`, `sheet`, `slides`, `image`, `data`. The artifact the person's latest message was sent with open is marked ` — open beside the chat`, and named first when it is not among the 8. `null` for a Helper and for a Conversation with no artifacts. A file whose studio turn is running is marked ` — being edited in its studio`. A studio session (ADR 0024) does not read this hint: its turn prefix carries one file line for its own group, resolved through the Conversation the way saves are (`chat_id`), since the route answers `null` for any child session today.

**Helper notices** ([ADR 0022](adr/0022-the-orchestrator-narrates-while-helpers-work.md)) (`engine/omnigent/superchat/helpers/notices.py`):

- *Tools.* Helpers only (offered when the session is a Helper, and allowed in the `worker` and `subworker` types): `report_progress({text})` and `raise_flag({text})`, one line each (whitespace collapsed, at most 240 characters).
- *Text forms* (final; the prompt and the web rely on them): `[helper <name>] progress: <text>` and `[helper <name>] flag: <text>`, where `<name>` is the Helper's title. The completion notice is unchanged.
- *Delivery.* Both are runtime notices (`is_system_notice`) posted to the parent through the engine's message path. A flag is posted plainly: steered into the parent's running turn, or a wake when it is idle. Progress is first posted with `if_running`, which only a turn streaming right now takes; when the parent is idle (the usual case, since its turn ends after it starts its Helpers) the progress wakes it, at most once every 30 seconds per parent, carrying every Helper's newest waiting line. A flag's wake and a completion notice carry the waiting lines the same way, ahead of the notice, and restart the 30 seconds. A finished Helper's waiting progress is dropped; its result says more.
- *Throttle.* A progress notice replaces the same Helper's undelivered one; a Helper's progress is steered at most once every 5 seconds, and one held back is sent when the interval ends. Flags are never held back or merged. The state is the runner's own and does not survive its restart.
- *Activity.* The two calls show as lines under their Helper: the progress text, and `Flagged: <text>`.
- *Measurement.* Two log lines with the session id: `first token for <session> <s>s after the turn started` (each turn in the Conversation; counted from the runner's turn start, after the engine's dispatch and any host wake, so it measures the model and the prefix, not the person's wait) and `first progress for <parent> <s>s after its helper started`. The person's wait is logged by the engine's relay ([ADR 0026](adr/0026-a-turn-speaks-in-a-second.md)), once per turn and counted from the moment the engine accepted the person's message that started it (an in-memory sub-second stamp the events route sets on accept; without one, after a restart or on another replica, the message's whole-second `created_at`): `first visible event for <session> <s>s after the message` (the first text delta, tool call or thinking block opening, cards included), `first word for <session> <s>s after the message` (the first text), and `no text for <session>: turn ended after <s>s without a word`. A turn with no new message of the person's (a Helper's notice) logs none of them. `engine/scripts/scenarios.py` reads these lines when `OMNIGENT_SCENARIO_ENGINE_LOG` names the engine's log, else coarse waits from the item timestamps, and fails a scenario over the budget.

## The check line

The plan of [ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md) is gone ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)): there is no `plan` card, no step state and no step comment. A Round's progress is the fold of its recorded events, and no task record is written: the work still under way is read from the Round record. What stays from ADR 0027 is the check.

**Check.** A Round that saved a deck or ran `deck_check` is checked when a `deck_check` in its record, the Assistant's or a Helper's, came back clean: no structured error (`{"error": …}`) and not `"ok": false`. The record's `tool.result` carries that as `ok`, from the relay; the runner posts nothing. The Round's journal entry says `Checked` or `Not checked`; a Round with no deck says neither.

**Transcript.** Each assistant message carries `activity_id` (`turn:<conversation>:<response_id>`), the Activity of the turn that wrote it. A `plan` card in an older chat is no card kind any more: it draws its fallback text. An answer card's `message` (its hand-back sentence) is drawn as the turn's text above the card when the turn wrote none of its own (`superchat/transcript/blocks.py`). A reply the person read live but the engine could not store is followed by a `reply_not_saved` error item, shown as a failure note and published live, so it does not vanish silently on the next read.

**Activity.** Each Activity dict gains `summary_line` (one line under the reply: "Created 2 files, searched the web, and 12 more steps"; files counted once per name saved, at most two kinds named, the rest "N more steps", cards not counted; `null` with no steps) and `families` (the tool families its steps used: `web`, `files`, `decks`, `shell`, `browser`). Each step gains `kind` (`file`, `web`, `brief`, `recall`, `read`, `helper`, `command`, `check`, or absent) and, for the brief, `snippet` (its first line). Titles: `context_brief` is "Checked what we already have", `artifact_outline` "Reviewed <name>" (else "Reviewed the file").

**Pause.** A held Helper's `session.status` carries `paused: true` beside `blocked_on`; otherwise `paused` is absent or null.

## The assistant narrates while helpers work

The rules the assistant bundle carries ([ADR 0022](adr/0022-the-orchestrator-narrates-while-helpers-work.md)):

- **Speak first.** One sentence on what it will do, streamed before the first tool call of any request that does work, a helper or not ([ADR 0026](adr/0026-a-turn-speaks-in-a-second.md)); never a question or a plan in text.
- **Progress is a sentence.** A helper posts a one-line progress notice to its parent at each step it completes. The parent narrates it in one or two sentences in its running reply. The helper's transcript never enters the parent's context.
- **Flags wake the parent.** When a helper chooses between sources, deviates from the ask, or finds something the person should know, it raises a flag; the parent tells the person right away.
- **Decisions, not mechanics.** "I'm using the ledger", not the command that read it. Surprises as soon as they are known. On hand-back, at most three things to look at; the rest goes in the artifact.
- **Questions are answered now.** A message while helpers run is answered on the main turn; the helpers keep going and the reply says so.
- **Drafts are versions.** A helper writing an artifact saves draft versions as it goes, so the work strip under the reply can show the artifact filling in.

Engine contract for this, additive: a `progress` and a `flag` notice kind beside the completion notice (`superchat/helpers`), a throttle of one progress notice per helper step, and the parent's wake handling narrating the notice in its running reply. Web: a work strip under the reply (label, elapsed time, Show details, the artifact strip when a draft exists) and helper cards attached to the message that started them. The activity panel is unchanged.

The web reads the strip from what already exists: the `helper` blocks of the reply (the web now keeps their `status` as the fallback for a Helper the activity feed does not list), and the Helper's Activity steps, where `report_progress` is the latest status line and `raise_flag` (`Flagged: <text>`) a flag shown at once. A draft is the newest version of a recent artifact whose `chat_id` is one of the reply's Helpers and whose origin is `ai` or `update`; it is looked up again on each `artifact_save` step. "Answered while the work continues" marks the first reply to a person's message sent while an earlier reply's Helper ran; the transcript item has no time to first token, so it carries no number.

## What is gone

Side chats, forks, the Chats popover, "Chat with this folder", the fork action on a message, the auto-archive setting, the `side_chat_open` tool, seeds and fork summaries. The removal and its commits are in the [trim log](engine-trim.md).
