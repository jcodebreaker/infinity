# A fork is a side chat with an anchor

> **Status:** Superseded by [ADR 0021](0021-one-conversation-messages-link-to-artifacts.md). Side chats and forks are removed; a message links to the artifact it is about.

People want to ask about one message without derailing the Conversation, the way a reply works in iMessage. We could build this as a new kind of chat or as a reply thread stored inside the Conversation. Instead, a Fork is a Side chat that records its anchor: the Conversation message it started from.

The engine owns the whole behaviour, so any client gets it (ADR 0009):
- It seeds the fork with the Conversation only up to the anchor.
- It stores the anchor on the side chat.
- It lists the forks under each message in the transcript, with their title, reply count and live flag.
- On request, it adds the fork's one-line summary back under the anchor as a Conversation item that the Muse can read.

Clients draw the reply line, the stacked pill, the thread view and the colors.

## Considered options

- **Replies stored inside the Conversation.** Rejected: every fork turn would grow the Muse's context, and the Conversation would stop being one readable record.
- **A new chat kind beside Side chat.** Rejected: it would duplicate everything Side chats already have (runner, seed, archive, transcript, live stream, unread) for one extra field.
- **Forks of forks without limit.** Rejected: a deep tree is hard to read on one screen. One extra level covers "ask about the answer"; anything deeper starts a plain Side chat.

## Consequences

- Side chats gain an optional anchor (message id), and the seed builder can cut at an item instead of at the latest one.
- The transcript's messages carry `forks: [{session_id, title, replies, live}]`.
- "Add to Conversation" is a Conversation item that points back to its fork, so it shows as a summary line and is in the Muse's context.
- Side chats without an anchor behave exactly as before.
