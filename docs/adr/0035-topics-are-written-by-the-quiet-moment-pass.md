# 0035: Topics are written by the quiet-moment pass

> **Status:** Accepted 2026-10-23 by the owner, from the clickable mock-up (Home topics, pick-up card, Topics panel). Defaults chosen: topics cover 7 days, Home opens on the last 48 hours, the panel lives in the dock, topic watch filtering decided here and built next. Builds on [0017](0017-the-working-list-is-tagged-and-the-journal-lives-in-the-host.md) and [0005](0005-proactive-work-is-scheduled-helper-runs.md).

One Conversation for a year leaves the person without the cues other chat apps give for free: a list of session titles to scan and the last few messages under each. People asked for those cues back ("where was I on Polaris?") without giving up the single Conversation. The data already exists: the quiet-moment pass writes today's note (*What we talked about*, *Decisions*, *Promised*, *Open loops*) 20 minutes after the person goes quiet, the working list carries `t-` and `o-` tags, and the journal has dated lines. Nothing shows it.

## Decision

**A topic is a record the quiet-moment pass keeps.** The same `notes` Helper run that updates the daily note also updates the person's topics with one Helper-only tool, `topics_update`. No new model call, no new schedule, no new gating: the pass already respects proactivity, quiet hours and its per-day cap. A topic is:

| Field | What it holds |
|---|---|
| `key` | Stable slug, reused when the topic comes back (the working list's `t-` tag when one exists) |
| `title` | 2 to 5 words, the person's terms, written like an Activity title |
| `left_off` | One sentence: where it stands now |
| `covered` | Up to 6 dated lines `{date, kind, text, file?}`, `kind` one of Decided, Made, Found, Asked |
| `open` | Up to 4 lines `{kind, text}`, `kind` Open or Promised (from the note's open loops and promises) |
| `starters` | 2 or 3 next requests in the person's voice, each one line, never sent on their own |
| `events` | Dated one-line moments `{at, line}` (newest 12 kept), the source of the panel's by-day list |
| `sources` | Counts of chats, files and emails it drew on |
| `watch` | `{people, terms, refs}` for topic watch (see Signal filtering); stored now, read by the next wave |
| `last_touched_at`, `status` | Newest event; `open` or `closed` |

The engine stores topics in one table per owner (`superchat/topics/`, its own capability folder: store, routes, tool, handler). `topics_update` upserts by `key`: a field passed replaces the stored one, `events` are merged and deduplicated, `last_touched_at` is the newest event. Code validates sizes and kinds and trims; it never asks a model to repair. A topic untouched for 7 days drops out of every list but is kept; a `closed` one is hidden.

**The pass sees what it is updating.** The quiet brief lists the person's current topics (key, title, `left_off`, last touched) so the Helper updates by key instead of inventing a near-duplicate. When the person has no topics yet, the brief also carries the last 7 daily notes, so the first pass after this ships seeds the list from history.

**The web shows them in two places.**

- **Home:** "Pick up where you left off" under the composer: one card per topic with when, a 7-day strip of days touched, title, `left_off` and source counts. A toggle switches between the last 48 hours (default) and this week. The home's contour ground lifts into a peak under each card, higher for recent and busy topics.
- **Topics panel:** a lightbulb in the dock opens a panel listing `events` newest first, grouped by day, so one topic can appear on several days. A row expands to its last covered lines and first open item, with *Pick up in chat*.

**Picking up stays in the one Conversation.** A pick calls `POST /v1/me/topics/{key}/pick`. The engine appends one `topic` card item to the Conversation (no turn starts, nothing is sent to the model now) and the web renders it: where we left off, what we covered, still open, and the starters. A starter fills the composer and the person edits or sends it. The card is part of the record, so the next turn sees what was picked, and recall pulls anything older. No session is created and no prompt is injected.

**Wording.** "Topics" and "Pick up where you left off". Nothing the person sees mentions the pass, a Helper or a tool.

## Signal filtering: how topic watch decides what is worth saying

Decided here, built in the next wave. Topics are the filter: a signal from mail, calendar, files or the web matters only if it moves a topic the person is working on. Everything else waits for the daily brief or is dropped. The pipeline narrows in five stages; the cheap stages are code, a model sees only what survives them, and the last word is a code rule.

**1. Watch list (written with the topic).** Each open topic carries a `watch` field the quiet pass fills alongside it: the **people** involved (names, email addresses), the **terms** that identify it (project, client, deal names; at most 8), and **hard references** (email thread ids, calendar event ids, file ids the topic already touched). Code adds references it already knows from the topic's sources. A topic closed or untouched for 7 days stops being watched.

**2. Collect (code, no model).** A hidden scheduled run of kind `topic_watch` (0005; hourly during the person's working hours, gated by proactivity and quiet hours like every proactive run) reads only what changed since its last cursor, per source: new mail, new or changed calendar events, changed files in the person's Computer, and the web only for topics marked as needing it (markets, news). Read-only; one cursor per source per person.

**3. Match (code, no model).** An item survives only if it hits a topic:

| Strength | Rule |
|---|---|
| Strong | It is in a watched thread, meeting or file (hard reference) |
| Medium | A watched person sent it, attends it or edited it, **and** it contains a watched term |
| Weak | It contains two or more distinct watched terms of one topic |

Before matching, code drops noise: bulk and no-reply senders, newsletters (list headers), auto-replies, the person's own sent mail, plain accept or tentative calendar responses, and anything already seen. Each item keeps the topic it matched; an item matching nothing is counted and dropped.

**4. Judge (Light model, structured, no tools).** Surviving items go in one batched call per run with each topic's `left_off` and open items. Item text is fenced as data, never instructions. For each item the model returns a fixed shape that code validates:

`{topic_key, relevance: 0-3, change: answers_open_item | new_deadline | blocker | new_info | none, urgency: now | today | brief, why: one line}`

**5. Decide (code rules).** Nothing reaches the person unless `relevance ≥ 2` and `change ≠ none`.

- **Message now** when the change answers an open item, blocks one, or brings a deadline inside 24 hours, and the urgency is `now`, and mark the topic (below) so its "New" line shows whether or not the message is read. Quiet hours hold the message until they end.
- **Mark the topic** in every other kept case: the topic gets its "New" line (the green tag on Home and in the pick-up card) and the item goes into the next daily brief. No message.
- **Limits:** one message per topic per 3 hours; per day at most 1 (low), 3 (balanced) or 6 (eager) by the proactivity level; the same thread never surfaces twice.

**Learning from the person.** A proactive message carries *Not useful*. Pressing it mutes that sender or term for that topic (a stored rule code applies at stage 3); acting on a message (opening, replying, picking up the topic) keeps its weight. Mutes are listed in Settings and can be removed.

**Every run is accountable.** Each run records its counts per stage (collected, matched, judged, surfaced, marked) and the reason for each surfaced item, so a quiet day can be told apart from a broken watch. The judge's call is in the model-call log like every other.

**Amended 2026-10-23 (owner): term specificity.** A watch term that is everywhere in the person's own world is not a signal: when a topic about their company overview deck watches the name of the organisation they work at, every work email mentions it and the rules above would hand nearly all of them to the judge. So code tells **common** terms from **specific** ones, per person, with no setting:

- **From what the person actually receives.** Each run counts, before matching, the items it collected (noise and web items left out, each item once): per watched term, how many items were seen while it was watched and how many contained it, kept per day for the last 14 days. A term is common when it is in at least 5% of those items, once at least 20 items were seen; below 20 only the seeds below apply.
- **Seeds, no counts needed.** The person's own organisation and email domain are common from the start: organisation names come from their standing memory facts that say where they work ("works at X", "employed by X", "is a VP at X"), and the domain from their own address when the engine knows it (consumer mail providers left out); a term that is the organisation's name, its leading words or its domain's name counts. A term watched by 3 or more open topics is shared, not distinctive. A short stoplist covers terms under 3 characters and generic work words (deck, report, slides, meeting, update, Q1 to Q4, email, project, draft, team).
- **Rules.** A common term never counts. Strong is unchanged; **Medium** is a watched person plus at least one specific term; **Weak** is two or more distinct specific terms. A topic whose terms are all common can still match by its hard references, otherwise not. Mutes are unchanged.
- **Good terms in the first place.** The quiet pass, the seed pass and the Conversation's rule for a picked topic tell the writer that watch terms must pick the topic out within the person's own work (a client, a deal or project codename, a file name), never their own organisation, team or anything in most of their mail. The quiet and seed briefs name the person's organisation, when known, as data so it can be left out. The wording lives in the prompts, never per company in code.
- **Accountable.** Each run's record lists the terms treated as common and why (`seed:org`, `seed:shared`, `stoplist`, or "23% of 140 items"), and each matched item's reason names the specific terms that hit.

## Considered options

- **Generate a topic summary when it is clicked.** Rejected: a model call per click is slow, and the pass already reads the same turns.
- **Cluster session history in the engine (embeddings).** Rejected for now: one Conversation has no sessions to cluster, and the notes Helper already names what was covered.
- **Put topics in the daily note.** Rejected: a topic spans days; the note is one day.
- **Extend `daily_note_update` with topics.** Rejected: two capabilities in one tool, and the folder rule says one capability per folder.
- **Open a new session per topic.** Rejected: it breaks the one Conversation (0021).

## Consequences

- An engine rebuild and a bundle render (the `notes` Helper gets `topics_update`); no host change.
- Topics appear only after a quiet pass has run; the first pass seeds from the last 7 notes.
- The pass's output now carries topics: health is visible in the model-call log of each quiet pass (topics written, keys reused vs new).
- Topic watch is decided above and built next; this wave already stores each topic's `watch` field, so the watch starts with history. The card already has a place for its "New" line.
- Mail and calendar signals need a connector with change cursors; until one exists, topic watch runs on files and the web only.

## Amended 2026-10-23 (build)

- **The next turn also gets a one-line note.** A warm session holds its own history and is sent only the person's new message, so it never reads a card the engine appended between turns. A pick therefore also marks the Conversation, and the next turn starts with one line naming the picked topic's title and key, cleared once read (the same one-shot pattern as the person moving a chat). The card stays the record; a session assembled from the record reads it there.
- **The pick is recorded outside any turn.** Its items carry a `recorded_` response id: no Activity row, no turn link, and the runner's crash recovery never starts a turn for them.
- **Home opens after a break.** The app opens on Home when the person returns after 4 hours or more away (and on a first visit); otherwise it opens on the Conversation as before. The Conversation has a Home button to go back.
- **Home is always the first screen** (owner, 2026-10-23), replacing the 4-hour rule above: every load opens on Home; "Continue the conversation" (or scrolling or swiping on past Home's end) slides the Conversation in, and its Home button slides Home back. A message link, a send from Home and a picked-up topic still land in the Conversation.
- **Back to Home is the logo** (owner, 2026-10-23): in the Conversation the superchat logo is a Home button; a pull past the top of the Conversation (scrolling on up or swiping down once the top is reached, held 400 ms first while earlier messages can still be loaded, with a slim "↑ Home · N topics" tab while pulling) or ⌘↑ (Ctrl+↑) also slides Home back, and ⌘↓ from Home continues. The text Home button is gone.

**Seed pass (owner, 2026-10-23).** A person with no topics should not wait for their next quiet moment to see Home filled. Once per engine process, for each person active in the last 7 days who has no topics, the quiet-moment job starts one `notes` run that reads the last 7 days of the Conversation and the last 7 daily notes and writes topics only (`SEED_PROMPT`); the same proactivity and quiet-hours gates apply.

**Amended 2026-10-23 (owner): Super Chat keeps a picked topic current itself.** A pick also marks the Conversation's active topic (key and pick time) until another pick replaces it or 3 hours pass. The Conversation gets `topics_update`, scoped to that topic: the engine refuses a call from the Conversation for any other key or after the 3 hours, and Helpers keep full access. The pick's one-line note is data only: which topic was picked and its state as last written (fenced as data). What to do with it lives in the Conversation's prompt template (`agents/templates/AGENTS.md`), not in engine code, so it can be tuned without a code change: when a turn finishes work on the picked topic, update it as the last call of the turn, after the reply and any follow-ups (a first live run updated the topic before saying anything; `suggest_follow_ups` then refused for lack of words and the turn ended with no reply) (what was done moves from open to covered, dated today; a new `left_off`, new starters, one event), written as short notes in the person's terms, never about "the user", and never mentioned to the person. The quiet pass's topic step carries the same wording rule. Home and the Topics panel re-read topics when shown, when opened and when a turn ends. No extra background run: the quiet pass remains the sweep for everything else.

## Amended 2026-10-25 (owner): Home tiles, status and tags

Home's row was a list of equal glass cards with counts, a week strip and a green "New" tag; the owner asked for a calm, editorial row that says in words what needs them, with nothing cropped.

- **Tiles.** "Pick up where you left off" (no count line) shows one **feature tile** and a tile per topic. Each tile is washed in the topic's own colour (two neighbouring hues derived in code from its key, the same on every client) and shows the topic's tag, its full title (never truncated), its status line and the day it moved. No icons, counts, badges or raw dates. The range toggle stays.
- **Which topic is featured.** The topic with work the Lookout prepared that the person has not opened: its newest proactive message carried a draft file (`made`) and is still after the person's seen mark. Else the topic whose dated promise is due soonest, today or already past. Else none, and every tile is the same size. The feature shows the message's first sentences (short by what they say, never cut by CSS), "Review the draft" (opens the file, and counts the message as seen and acted on) and "Pick up".
- **Tag** (new optional field `tag`, 1 or 2 words): written once by the topic writer (the quiet pass, the seed pass, the Conversation's picked-topic rule). Without one, code takes the first word of the title that names something.
- **Status line** (new optional field `status_line`, one sentence of at most 60 characters, Duet style: "Dana is waiting for the link"): written by the topic writer whenever the topic moves, and by the Lookout after a posted wake (its Result gives one `status <key>: <line>` per topic it worked on, never posted). `status` keeps its meaning (`open` or `closed`). Without a line, code words the next open item ("Send the link · due Friday"), or "Done. Nothing open".
- **Prepared draft** (`prepared`, written by the Lookout only): the posted message's id, the draft's file and the message. The topics list serves it only while the message is unseen (the outbox tells, through core).
- **The "New" line words its date** ("Send the link · due today"), never "Due 2026-10-25". The wake prompt says what was checked and prepared, never how or what could not be done.
- **Greeting.** Home greets by first name only when the account name is a real one, never a placeholder such as "local" or a login id.


## Amended 2026-10-25 (owner): a calmer pick-up card

The pick-up card felt overwhelming: seven blocks, and a promise said up to four times (the summary, the "New" bar, its open line, its starter). It is now read in two seconds and the rest opens on demand.

- **Layout.** "Where we left off" with the last-touched time, the title and `left_off`; one **Next** row (the open line due today or already past, else the first Promised, else the one due soonest, else the first) with its kind, due words and one button; **Also open**, the other lines one per row, each with a small button; and **What we covered · N steps · M files**, folded. The "New" bar, the separate starters section and the source counts are gone.
- **Starter and verb on open lines** (new optional fields): each open line carries its `starter` (the request, in the person's voice) and `verb` (1 or 2 words for its button: "Draft it", "Fix"). A line that keeps its id keeps them, like `due`. A button fills the composer, never sends.
- **Older topics.** `starters` stays, for next steps no open line covers. A line without its own starter takes the loose one it shares at least two content words with; what is left joins Also open as "Start" rows, so nothing is lost.

## Amended 2026-10-25 (owner): work through all

- **One button.** With two open rows or more, the pick-up card shows "Work through all N" below Also open. Unlike the row buttons it sends at once (the click is the ask), then reads "Working through N" and is disabled.
- **The message.** "Work through everything still open on <title>", then the topic's key and one line per row (kind, id, due, its request). No channel carries hidden data with a message, so the text holds it all.
- **The Conversation** (prompt only, `agents/templates/AGENTS.md`): one Helper per line at once; a send, share, payment, publish or delete is drafted as a file only; a decision returns a recommendation and acts on nothing. When all are back: one sentence, a ✓ line per item, the topic updated, then the decisions one at a time as question cards, recommended option first (an `ask` card holds one question and ends the turn).
- Progress is the Round's strip and Activity, one row per Helper; nothing new there.

## Amended 2026-10-25 (owner): the pick card is never older than the topic

A Lookout post set the status line and the prepared draft but left `left_off` as the quiet pass had written it earlier, so a pick a minute later showed the older summary. Now the Lookout's post also writes where the topic stands, by code: the message's first sentence becomes `left_off` and a dated event, and `left_off_at` stamps every write of `left_off` or `status_line` (any writer). Code rather than a new Result line, so it holds without the model and needs no prompt. A background pass never replaces where a topic stands with a view older than it: a write from a Helper that started before `left_off_at` keeps the stored `left_off` and status line (the rest of its write lands). The pick card leads "Also open" with "Review the draft" (opens the file) while the draft is still the newest thing (`prepared.at ≥ left_off_at`): recency, not the seen mark, since a draft read in the chat is still waiting; any later write of where it stands retires the row.
