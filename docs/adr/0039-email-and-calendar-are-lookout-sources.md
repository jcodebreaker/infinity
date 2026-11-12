# 0039: Email and calendar are Lookout sources

> **Status:** Proposed 2026-10-25 by the owner's request after the Home redesign. Builds on [0038](0038-one-lookout-per-person-wakes-on-signals.md) (Lookout, signals, work queue) and [0036](0036-proactive-messages-go-through-one-outbox.md). Two choices are left to the owner (end of this record).

The Lookout does real work when something happens, but today only two things can happen: a live page refreshes, or a promise the person told the assistant about comes due. The assistant learns that "Dana is waiting for the link" only because the person said so. A proactive assistant earns its place by reading where work arrives, the inbox and the calendar, and by preparing the reply before the person asks. 0038 left a source interface for exactly this; nothing plugs into it yet.

What the code already has: a per-person OAuth framework ("Connections": signed state, callback, status, disconnect) with tokens sealed in KMS or Vault Transit and refresh handled (GitHub only); a server MCP pool the engine can call without a model turn; Google tool policies that deny send by default; the approvals policy that classifies a send and asks; the work queue with `Defer`. There is no Google or Microsoft client, no mail or calendar code and no webhook route.

## Decision

**Email and calendar are two Lookout sources, one capability folder each** (`superchat/mail/`, `superchat/calendar/`), with one provider adapter per service inside each (Google: Gmail and Google Calendar; Microsoft 365: Outlook mail and calendar through Graph). The Lookout does not change.

**Tokens live in Connections.** Google and Microsoft are added as providers to the existing connections registry: the same connect, callback, status and disconnect flow, the same sealed store and refresh. Never the Vault (website logins only) and never the host. App registrations are deployment-wide, like the GitHub App; a tenant admin can grant consent once for an organisation (Microsoft) or a workspace (Google).

**Read first, narrow scopes.** The first version asks only for read access (mail read, calendar read). Anything that writes is a later, separate consent (below).

**Polling by code, through the work queue, no model.** Each connected person has one `mail_poll` and one `calendar_poll` job that re-defers itself (every 10 minutes while the person's hours are open, every 60 outside them, jittered). Each poll reads changes since its cursor (Gmail history id, Graph delta link; calendar sync tokens), so a quiet inbox costs one small request and no model call. Provider push (Gmail watch, Graph subscriptions) needs a public webhook route and is a later optimisation behind the same source.

**Code decides what becomes a signal.** A mail is a signal only when it touches the person's work, decided without a model:

- it is in a thread the person wrote in, or from a person on an open topic's watch list (0035), or it names a topic's watched terms or files;
- it is addressed to the person directly (not a list, not a bulk or automated sender: list headers, `noreply`, marketing categories are dropped);
- one signal per thread (dedupe key `mail:<thread id>`); a newer mail in the thread updates it.

Calendar signals:

- a meeting in the next working day that matches an open topic: a `normal` signal due the evening before or 2 hours before, so the Lookout can prepare a short pack;
- a meeting starting within 30 minutes that matches an open topic: `now`;
- an invite waiting for the person's answer, a meeting moved or cancelled that touches a topic: `normal`.

Matching reuses the topic matching rules already in the Lookout (strong / medium / weak, common terms filtered).

**Keep as little as possible.** A signal stores sender, subject, time, the thread or event id and an excerpt of at most 800 characters, and is deleted with its signal once settled. No mailbox copy, no attachment copy, no mail in the journal or the recall index. When the wake run needs more, it reads the thread or event through one read-only tool (`mail_read_thread`, `calendar_read_event`), served by the engine with the person's connection and visible in the run's record. Disconnecting deletes the token, the cursors and every waiting mail or calendar signal.

**The Lookout prepares; the person sends.** The wake run can draft the reply as a file the person reviews ("Review the draft"), as today. Writing into the mailbox is a second step with its own consent:

- **Draft in your mailbox:** the reply is saved to the person's Drafts folder in the thread, ready to send from their own mail app. Needs compose scope; asked through the existing approvals (category send) the first time, then by a standing rule if the person allows it.
- **Send for me:** sending is always a per-message approval card (the assistant shows recipient, subject and body and waits). Google's send policy stays deny unless that approval says yes. This step ships last.

**What the person sees.** Settings gains a Connections section: Connect Google / Connect Microsoft 365, what each reads, last check, Disconnect. Home and the brief show the work, never the mail itself ("Dana replied about the Morocco pages. **Your answer is drafted.**"). Copy reads "from your chats, files and mail" again only once mail is connected. The Settings section needs a clickable mock-up and the owner's approval before it is built.

## Considered options

- **The person forwards mail to the assistant.** No consent screens, but nothing arrives on its own; it is not proactive.
- **A third-party MCP connector per person.** Fast to demo, but tokens and policy would live outside our sealed store and the engine could not poll without a model turn.
- **Read mail on the host.** Keeps data at the edge, but every poll would wake a host; the engine poll is one cheap request and keeps only an excerpt.
- **Push first.** Fresher, but needs a public webhook route, renewal of subscriptions and a queue for bursts; polling every 10 minutes matches what the person tolerates (Duet checks every few hours).
- **Let the model triage every mail.** Most mail is noise; a model per mail is the cost problem 0038 removed.

## Consequences

- Engine: two providers in the connections registry, two feature folders with provider adapters, two poll job kinds, two read-only tools, signal kinds `mail` and `calendar` with their own `stale_after` (mail: end of the next working day; calendar: the meeting's start), retention on settle. Web: the Connections section in Settings, mail tags in tiles and the brief. No host change.
- Deployment: one OAuth app per provider (client id and secret as deployment configuration, never in the repo), redirect URIs per environment, admin consent documentation.
- Privacy review before any real mailbox is connected; the train stack uses test accounts only.
- Steps: Microsoft or Google read (one provider end to end, train stack, model-call logs), the other provider, calendar, drafts in the mailbox, send with approval, push.

## Left to the owner

1. **Which provider first?** Recommended: **Microsoft 365**, since the product is for enterprises and most of them run on it; Google second.
2. **Is "draft in your mailbox" part of the first version?** Recommended: **no**. Ship read plus drafts as files first, then mailbox drafts once reading is proven quiet and accurate.

## Amendments

**Amended 2026-10-25: File mailbox for development.** Before any real provider, the mail source is built end to end against one stand-in, so the flow (new mail, poll, triage, signal, Lookout wake, the run reads the thread and drafts the reply, the message posted by code with the draft's card, outbox, Home) can be proved without OAuth or a real inbox.

- **What it is.** A markdown file standing in for one person's inbox: optional front matter (`owner`, `me: <address>`), then one `## ` section per message with `From:`, `To:`, `Cc:`, `Date:` (ISO with a time zone), `Subject:`, `Thread:` and optional `List-Id:` / `Precedence:` lines, a blank line and the body. Appending a section is a new mail arriving. A message's id is a hash of its thread, date and sender. The format is documented in `superchat/mail/`; `deployment/dev/mailbox.example.md` is the example.
- **Off unless asked for.** It exists only while `OMNIGENT_DEV_MAILBOX` names the file inside the engine (`deployment/compose.yaml` passes it through, empty by default). It is never a production provider and never connected to a real mailbox. The mailbox belongs to the front matter's `owner`, else the single local person.
- **Same source, same rules.** `superchat/mail/` is provider-neutral (message, thread, provider interface, triage, poll, the read tool); the file mailbox is its first provider and Microsoft 365 / Google plug in beside it. The poll is the `mail_poll` work-queue job described above; its cursor (the ids already seen) lives in the job's own payload, so no table is added. `OMNIGENT_MAIL_POLL_SECONDS` fixes the pace for development. A message older than 14 days when first seen is only marked seen.
- **What the build settled.** Triage asks the Lookout's own code check (`check_item`, through the composition root) for noise and topic matching; a sender on a topic's watch list is enough by itself for mail (`sender_match`, weak), and `notifications@` joins the no-reply senders. A thread the person wrote in that matches no topic is still a signal, marked `direct`, and the Lookout keeps it without a topic. When the person's own message is the thread's newest, the waiting signal is removed. Settled `mail` signals are deleted, not kept the 60 days other kinds are. `mail_read_thread` is offered to background runs and served by the engine only for a run of the caller's own whose task is the Lookout wake or the brief; the wake prompt tells the run to read the thread, draft the reply as a file and close with "Review the draft", never to send.
