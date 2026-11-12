# One conversation: messages link to artifacts

> **Status:** Done 2026-10-17, amended by [ADR 0024](0024-the-artifact-studio.md) (edits made with a file open go to its studio chat): side chats and forks removed, the chip, the hint, link-on-save, relink and `all_versions[].item_id` built on both sides. Contract: [`docs/conversation.md`](../conversation.md).

> Supersedes [ADR 0010](0010-a-fork-is-a-side-chat-with-an-anchor.md).

Side chats and forks gave a person a second place to talk: a chat seeded from the Conversation, or a reply thread anchored to one message. In practice the second place was used for one thing, changing an artifact without derailing the main chat, and it cost a second conversation to find, a seed to maintain, summaries to fold back, and 5,000 lines of web code. The canvas editor already solved the precise form of that need: select an element and the composer carries a pointer chip (`Slide 4 · growth-title`), and the request goes to the assistant with a hard scope so only that element changes.

There is one conversation. The assistant knows which artifact a message is about.

## Decision

- Side chats and forks are removed: `superchat/side_chats` (seeds, forks, routes, the fork summary prompt), the `superside-chat` seed plumbing in `context/labels.py` and `context/rollover.py`, and `web/features/side-chats` with its contract and client.
- The element chip the editor attaches stays as it is. When a message arrives with nothing selected, the assistant resolves which artifact it means, in this order: the artifact open in the panel; the one it just made or just edited in this conversation; one recalled by name through memory and recall. The message is shown with the same chip, filled in. A wrong guess is one tap to fix on the chip.
- Every save is a version, whether made by hand in the editor, by the assistant from a message, by a restore or by an undo. Opening an artifact lists its versions with who made each and the message that led to it. That list is a view over the one conversation and the version store; nothing is kept apart from them.
- The activity panel and the canvas editor are unchanged.

## Consequences

- The engine gains one capability: artifact resolution for a message. It is a tool-side lookup (open artifact, recent artifacts in the session, recall by name), not a new model call, so the first words of the reply are not delayed.
- Version entries gain an optional link to the message item that produced them. The version menu shows it.
- Old side chats and forks are not migrated; the database is fresh at the time of the change ([ADR 0020](0020-the-engine-keeps-only-what-superchat-runs.md)).
- `docs/conversation.md` describes the one conversation, helpers, the chip and the version view, and replaces the side-chat and fork sections of the earlier docs.

## Considered options

- **Keep forks as a lighter "reply" that stays inside the conversation.** Rejected: once the chip resolves the artifact, a reply anchor adds nothing the version link does not already give.
- **A thread per artifact as its own conversation.** Rejected: it recreates the second place to look, which is the cost this record removes.
