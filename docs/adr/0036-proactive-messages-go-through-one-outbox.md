# 0036: Proactive messages go through one outbox

> **Status:** Accepted 2026-10-23 by the owner ("fire all cylinders" after 0035). Builds on [0005](0005-proactive-work-is-scheduled-helper-runs.md) and [0035](0035-topics-are-written-by-the-quiet-moment-pass.md).

Super Chat already speaks unprompted: a scheduled run's result wakes the Conversation and Super Chat writes to the person. Nothing marks that message as unprompted, nothing tells the person it is there when the tab is in the background, and the only place it can appear is the web chat. The team wants the same message to reach Webex, Slack, email or a phone later without each source learning each channel.

## Decision

**A proactive message is an ordinary Conversation message with a label.** When a turn was started by a scheduled run, topic watch or the daily brief (not by the person), the assistant message it produces carries `proactive: {kind, topic_key?, urgency: now | today | brief}`. The words stay the harness's (0029). Nothing else about the message changes.

**One outbox, many readers.** Every proactive message also writes one outbox row: `{owner, item_id, kind, urgency, title, line, created_at}`, where `title` and `line` are code-cut from the message (first heading or sentence, at most 80 and 160 characters). Each channel is an adapter that reads rows after its own cursor `{channel, owner, cursor}` and delivers them its way. The engine never knows how a channel delivers; a channel never reads the Conversation. This wave builds the outbox and the `web` channel only; each other channel (Webex bot, Slack, email digest, PWA notification) gets its own record when built.

**The web channel is "you have something new".**

- The engine keeps `last_seen_item_id` per owner for the Conversation; the web reports it when the chat is visible and scrolled to the end.
- Unread proactive messages (outbox rows after it) show as a dot on the Super Chat dock button, a count in the tab title ("(2) Super Chat") and, in the chat, one divider above the first unread: "While you were away".
- A proactive message shows its kind in a quiet label ("Topic update", "Morning brief", "Automation") and two actions: **Open topic** (when it has a `topic_key`; uses 0035's pick) and **Not useful**.

**Not useful is a stored rule, not a vibe.** It records `{owner, item_id, topic_key, reason?}`; topic watch (0035, stage 3) turns it into a mute for that sender or term on that topic. The person sees the mute in Settings and can remove it.

**The Inbox keeps decisions; history moves under it.** The Inbox sheet keeps *Waiting on you* (today's asks) and gains *History*: answered asks of the last 30 days with who decided and when, read from the stores that already hold them.

## Considered options

- **A separate notifications store with its own text.** Rejected: two copies of what was said, and a second writer of the person's words.
- **Push from each source to each channel.** Rejected: N × M wiring; the outbox makes it N + M.
- **Browser push now.** Deferred: a PWA service worker is its own record; the tab title and dot cover an open tab.

## Consequences

- Engine rebuild (two tables, routes, labelling at the turn start) and web build; no host change.
- Every new proactive source must set the label; a missing label means no outbox row and no dot, which the run record makes visible.

## Amendments

**Amended 2026-10-23 (build).** "Read from the stores that already hold them" was not true when this was built, so the stores now keep what History reads:

- **Approvals keep the answered row.** `approval_pending` gains `status` (`pending`, `approved`, `denied`, `expired`), `decided_by` and `decided_at`; an answer marks the row instead of deleting it, and a prompt nobody can answer any more reads `expired`. Everything that asks what waits (the pending lists, the asks, the restart restore, the answer path) reads `pending` rows only, so a waiting approval behaves as before (`superchat/approvals/store.py`).
- **A replaced proposal is `superseded`.** A newer plan proposal used to mark the open one `dismissed`, like the person's own dismissal; it is now `superseded`, and `dismissed` means the person dismissed it (`stores/objective_store`). Rows from before keep their status.
- **Blocked-task answers are not in History.** The answer lives in the task's note with no time of its own, so History cannot place it in the last 30 days.

History is `GET /v1/me/asks/history`: approvals approved or denied and proposals accepted or dismissed in the last 30 days, newest first, each with who decided and when.
