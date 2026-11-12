# 0038: One Lookout per person wakes on signals

> **Status:** Proposed 2026-10-25, design settled with the owner question by question. Builds on [0035](0035-topics-are-written-by-the-quiet-moment-pass.md), [0036](0036-proactive-messages-go-through-one-outbox.md) and [0037](0037-the-daily-brief-is-a-scheduled-page.md); replaces 0035's topic watch.

Super Chat speaks up through separate timers that do not know about each other: the quiet-moment ticker every 30 seconds, the topic seed every 15 minutes, topic watch every working hour per person, the daily brief at 7:30 for everyone, the evening and nightly passes and hourly memory upkeep. Each person costs about 8 model runs a day even when nothing happened, everyone wakes at the same minutes with no limit across people, every person gets the same "3 messages a day", and topic watch only sees Library files, found by polling. As the number of people grows, the engine takes every rush at once.

## Decision

**One Lookout per person owns everything that can message the person unasked.** Tidy-ups that never speak (quiet-moment notes, the nightly pass, memory upkeep) keep their own timers but run through the work queue.

**One work queue for every background model run.** A Postgres table in the engine, claimed with row locks and a lease so a run survives a restart and never runs twice. It has one limit across all people and keeps today's per-person limit (3, lower while the person's own turn runs). It extends the per-owner queue automations and the brief already use; a separate worker can read the same table later.

**Signals, pushed by sources.** A signal is one thing that might be worth telling the person: `{owner, kind, topic_key?, subject, due_at, stale_after, urgency, dedupe_key}`. Sources push through one entry point and never call a model; each source is its own capability folder. A source that cannot push runs its own small model-free check through the work queue at its own pace. The dedupe key folds repeats (one file saved five times in ten minutes is one signal).

- First sources: **Library files** (the engine adds a signal when an artifact version with origin `update` is written; no polling), **promises coming due** (below) and the **morning brief**.
- Mail, calendar and web news get the same interface and are built later, each with its own record.

**The Lookout never wakes on an empty tray.** Time-based things are signals with a future `due_at`. The Lookout wakes at its earliest due signal or when a new one arrives; never to look around. A quiet day costs no model run.

**A wake: code first, then rhythm, then one model run.**

1. Code check: match signals to topics with topic watch's rules (strong / medium / weak, common terms filtered, the topic's watch list), apply mutes, drop duplicates and noise.
2. Rhythm check (below): is now a good moment for this person?
3. One Builder run sees everything left and writes at most one message, or decides there is nothing worth saying. It may read files and topics and prepare a draft (a reply, a slide, a summary) attached to the message; it never sends, shares, pays, deletes or changes anything outside its own draft. Those wait for the person's yes in the Conversation.

The message goes to the outbox (0036) unchanged.

**Two urgencies.** `now` wakes the Lookout at once and skips the rhythm check, still honouring mutes and quiet hours. `normal` goes through the rhythm check. Code sets urgency from the signal's kind (meeting soon, deadline today, a blocker), never the model. This replaces 0036's `now | today | brief` for Lookout messages; the brief keeps its own kind.

**Not a good moment: hold.** The Lookout sets its next wake to the person's next good slot and looks at everything waiting then. A signal past `stale_after` (default: end of the next working day) is never sent as a message; it only sets the topic's "New" line (0035's MARK).

**The brief is a promise.** It is a signal due at the person's own time (default 7:30), spread about ±10 minutes across people, skips the rhythm check, is always delivered (shorter on quiet days) and folds in the signals waiting at that moment.

**Promises carry a date.** Open/Promised lines on a topic gain an optional `due` (YYYY-MM-DD) written by the topic writer (the quiet-moment pass or the Conversation). Code turns each dated promise into a signal due 9:00 that day (the working day before for an early deadline) and updates or removes it when the date changes or the promise closes. A promise without a date gets no reminder.

**Rhythm: learned from behaviour, never from ratings.** Per person, a tally per situation. A situation is the hour of the day (weekday and weekend apart) × what the person is doing: chatting (a turn is running), quiet (the app is connected), away (neither). What the person is doing is read live at wake time from what the engine already knows; no presence history is stored.

- The web reports four events per Lookout message: shown, opened, dismissed, acted on (tapped through to the topic or file). Any client can report them.
- Acted on, or the topic touched in the Conversation within a few hours: strong yes. Opened hours later: weak yes. Shown and dismissed within seconds: no. Unseen after 24 hours: weak no. Seen without action: neutral.
- **Not useful** stays a mute (0036) and does not move the rhythm: it says what to stop, not when.
- Until about four days of behaviour exist the rhythm uses today's rules: the person's level (off / low / normal / high = 0 / 1 / 3 / 6 a day), weekdays 8:00–18:00. The person's level stays a hard ceiling the rhythm never exceeds, as does one message per topic per 3 hours.
- Settings shows one plain line ("I usually reach out on weekdays around 9:00 and 16:00, rarely while you're chatting") and **Start over**, which clears what was learned. No grid, no hand edits.

**Topic watch is replaced.** Its hourly hidden task is removed (existing tasks deleted in a migration), with its cursor table and its Light judge. Its matching rules, watch lists, mutes and per-topic limit move into the Lookout's code check. `superchat/topic_watch/` shrinks to the Library-file source; the Lookout lives in `superchat/lookout/`. Switched over on the train stack and checked with the model-call logs before anywhere else; never run side by side.

## Considered options

- **Keep timers, add a queue.** Fixes the rushes, not the runs on quiet days or the one rule for everyone.
- **The Lookout pulls from every source on each wake.** More sources would mean more wakes and every source learning the Lookout's schedule.
- **One model run per signal.** More runs and up to one message per signal; one run per wake can connect related news.
- **Light, or Light then Builder.** Light writes poorly in the assistant's voice; two runs for what one can do.
- **Explicit 👍/👎 as the rhythm's input.** Most people never rate; behaviour is there for every message.
- **Topic in the situation.** Splits the history too thin to learn; what to hear about is already mutes and topic matching.
- **Store presence history.** A record of when someone is online, for little gain over reading it live.

## Consequences

- Engine: the work queue table, the signals table, the rhythm table, the Lookout service, the Library-file signal on artifact write, promise signals on topic write, four event routes, a migration removing topic-watch tasks. Web: report the four events, the Settings line and Start over. No host change.
- Steps, each checked on the train stack before the next: work queue (no visible change), signals and the Lookout replacing topic watch (no visible change), promise dates, rhythm, more sources.
- Topic gaps from 0035 (the 7-day window, the quiet pass rewriting covered and open, merging two topics) are a separate amendment to 0035.

## Amendments

**Amended 2026-10-25 after review, before build.** An independent review against the code found gaps; the owner said build ("fire all cylinders"), so these are settled here:

- **One run, posted by code.** The wake run is a hidden scheduled Helper (kind `lookout_wake`) on the person's host, because reading files and preparing a draft need the host. Its Result does not wake the Conversation: code posts it as an assistant message with the proactive label (0036) and writes the outbox row itself, the way the pick card's recorded items are posted. For Lookout messages this replaces 0036's "the assistant writes the words in a second turn"; the Builder run is prompted in the assistant's voice. A Result that says nothing worth saying posts nothing.
- **The brief keeps its Automation.** 0037's task stays (the person edits its time, Refresh now). Its RRULE minute is spread per person (derived from the owner, within ±10 minutes of the chosen time). When it fires, the Lookout claims the signals waiting at that moment and puts them in the brief run's task; the brief's Result is posted by code as above. One run, no separate brief signal.
- **Promises have ids.** Open lines become `{id, kind, text, due?}`; code assigns the id on first write and keeps it when the writer passes it back. The quiet-moment pass is now shown the whole topic (covered and open, with ids), which also closes 0035's overwrite gap. A promise's signal is keyed by `promise:<topic>:<id>`; rewording keeps the reminder.
- **A wake is claimed, not leased by time.** Claiming binds the signals to the run id. A failed or reaped run releases them; they are marked sent only when the message item is stored. A restart therefore never sends twice and never loses a signal.
- **Two states, not three.** What the person is doing is `chatting` (a turn is running, from the stored live status) or `not`. Presence in memory is per process and missing in single-user mode, so "quiet" vs "away" is dropped.
- **Coarser situations.** Four day-parts (morning 6–11, midday 11–14, afternoon 14–18, evening 18–23) × weekday or weekend × the two states: 16 cells.
- **Quiet hours default.** Unless the person sets otherwise, the Lookout messages only weekdays 8:00–18:00 in the person's time zone, `now` signals included; outside them a signal waits.
- **Behaviour events.** The web reports `shown`, `opened` and `acted_on` per Lookout message (the pick route carries the message's `item_id`). "Dismissed" is dropped until a dismiss control exists; unseen after 24 hours is the weak no. Outbox urgency keeps 0036's values: `normal` is stored as `today`.
- **`stale_after` per kind.** Library file: end of the next working day. Promise: the promise's date. `now` kinds: two hours.
- **Library signals as today.** Live-page refreshes (`origin=update`) are the news; the person's and the assistant's own saves are not. Other file arrivals get their own source later.
- **The Settings line and Start over** wait for a clickable mock-up and the owner's approval; the engine side ships first.
- **Correction.** A quiet day today costs about 2 runs per person, not 8 (topic watch skips the model when nothing matches). The case for the Lookout is the unlimited rushes, the brief's double run, one rule for everyone and missing reminders.

**Amended 2026-10-25 while building the work queue.** What the code asked for:

- **Leased until bound, then bound to the run.** A claim carries a ten-minute lease (renewed by the live process) only until its handler binds the run it started (a Helper's session id); from then the job holds the owner's slot until that session's turn ends (the terminal status edge) and no lease applies. A claim whose process died returns to the queue when its lease ends.
- **The queue has its own reaper.** The run reconciler is lazy on read, so it cannot free a slot nobody looks at. The worker reaps once a minute: dead claims back to the queue, runs whose session ended (or is gone) finished, runs past the reconciler's six hours failed; the lazy reconciler also releases the job of each run it force-fails.
- **The limits cover every queued run.** Quiet-moment passes and memory upkeep count toward the owner's limit too (today's limit counted scheduled Helpers only); the limit is still 3 (or the host's room, or `OMNIGENT_AUTOMATION_MAX_CONCURRENT`), one while the owner's turn runs. The global limit is `OMNIGENT_WORK_QUEUE_MAX_RUNNING`, default 16.
- **Busy no longer re-fires, it waits.** A queued kind (automation, live page, brief) waits in the queue instead of re-firing every 5 minutes, so it no longer leaves `owner_busy_deferred` rows. Any other kind starts only while none of the owner's runs is in flight and is skipped as `owner_busy` once it waited past the old window (3 x 5 minutes). Quiet-hours and per-kind deferrals and the 10-minute retry after a failed launch are stored on the job, so a restart keeps them.
- **Out of scope for this step.** Free-standing automations (no parent Conversation; no per-owner gating today), the fold and background titles keep their paths.

**Built 2026-10-25 (promise ids).** Settled in code: an id is 6 hex characters derived from the line's text, so a row written before ids reads with the same id every time and keeps it on its next write. A writer's id is kept only when it is one of the topic's; a line sent without one takes the id of a stored line with the same kind and text, else a new one. A line that keeps its id and passes no `due` keeps the stored `due` (a reminder is not lost to a forgetful rewrite). The write's result lists each topic's open lines with their ids, so a writer can pass them back in the same turn. The quiet-moment pass and the picked-topic note show open lines as `Promised: Send the deck (id 3f9c1a, due 2026-10-28)`; the pass sees this week's topics whole, fenced as data.

**Amended 2026-10-25 (Rhythm build).** What the code settled for `superchat/rhythm/`:

- **Which cell a message scores in.** Its send time in the person's zone and, when the poster records `sent` with the state it checked, that state; else `not` (today a proactive turn never starts while the person's own turn runs). Night (23:00–6:00) is no cell.
- **Scoring.** Each message adds once, from the strongest thing that happened: acted on +1 yes, opened +0.5 yes, unseen after 24 hours +0.5 no, shown only nothing. Counts halve every 14 days, so a closed cell fades back to today's rule and is tried again. A cell's rate is `(yes + 2p) / (yes + no + 2)`, `p` the person's overall rate under Beta(1, 1); a cell with less than 1 of evidence keeps today's rule; learned cells count after 4 days with events; below 0.35 says no.
- **Only quieter.** The hard window is the person's own hours (outside their quiet hours) when set, else weekdays 8:00–18:00; the level caps the day (the brief not counted); one message per topic per 3 hours via an optional `topic_key` on `good_moment` / `next_good_slot`. Learning only closes cells inside that window. `next_good_slot` returns eight days on when nothing opens (level off).
- **Events.** The web reports `shown` (on screen in a visible tab), `opened` (stayed there 3 seconds) and `acted_on` (a link or button inside the message). Open topic's pick carries `item_id`; the topics capability tells core `topic_touched` on a pick and on the Conversation's own update of its topic, and every proactive message on that topic of the last 3 hours counts as acted on. Unseen is a lazy sweep run before each decision, not a timer.
- **Reading the outbox.** The outbox registers a proactive-message reader in core; the rhythm never imports it.
- **Exploration floor (after review).** Closed cells get no new evidence, so someone who reads rarely would stay silent for weeks. When no counted message went out in the last 3 working days, the in-window cell with the best smoothed rate is allowed again, inside every ceiling: at most one exploratory message per 3 working days.
- **Unseen while offline still counts.** The engine cannot cheaply tell that the person had no web connection in the 24 hours (presence is per process and absent in single-user mode), so unseen stays a weak no for now.

**Amended 2026-10-25 during the build.**

- **The Lookout does the work, not a relay.** The wake run never says "you got an email" or "the deck changed". For each signal it asks what it means for the person's open topics and promises and what it can do now, does that work into a new draft file first (the reply, the new numbers, the three-line difference and what it breaks, the slide update; never the person's own file), and leads its message with the so-what and the prepared work, then one next step ("Send it?", "Open the draft"); the fact itself is at most half a sentence. Several signals on one topic are one piece of work. Saying nothing is the default: code then only marks the topic "New". The Result opens with `kind_of_value: draft | analysis | reminder | none`, logged and never posted, so the logs show how often it works and how often it would only relay.
- **How "posted by code" is kept from waking the Conversation.** A code-posted run (`lookout_wake`, and the brief's) is created with the ad-hoc Helper label carrying its task id instead of the scheduled one, so the runner never registers its Result for a parent wake (a server-side drop of the wake would leave the runner's wake flag and inbox stranded). The fire path tells the kind's hook of every run row and terminal edge (`register_run_result`); success posts the Result, anything else releases the claim. The brief's tool gate on the runner accepts either label, so this step needs a host rebuild after all.
- **The hidden task is paused and fired by run-now.** One `lookout_wake` task per person, never armed by the scheduler; a retry the fire path arms finds it paused and does nothing, so a failed run only releases its signals and the Lookout tries again ten minutes later.
- **A burst is one wake.** A `normal` signal wakes the Lookout two minutes after it arrives (a `now` one at once), so a page refreshed five times, or three files of one page, are checked together.
- **"Early deadline" in code.** A promise due on a non-working day is reminded at 9:00 the working day before; otherwise at 9:00 on its date. Its signal goes stale at the end of that date.
- **A run that never reports** is released after two hours by the Lookout's own sweep, until the work queue's reconciler owns leases.

**Merged 2026-10-25.** The four builds (work queue, promise ids, rhythm, the Lookout) were joined into one design:

- **Every ceiling lives in the rhythm.** The level (`off` never speaks), the hard window (the person's quiet hours, else weekdays 8:00–18:00), the level's daily cap (the brief not counted) and one message per topic per 3 hours are checked only by `superchat/rhythm/`; the Lookout's own cap, topic window, quiet-hours check and wake ledger are gone. The Lookout asks `good_moment(owner, at, state, topic_key=..., urgent=...)` per topic; `urgent=True` (a `now` signal) checks the ceilings only, never what was learned. A topic that may not speak waits until `next_good_slot`. The composition root hands the rhythm to the Lookout (`lookout.set_rhythm`), so neither imports the other.
- **The message's `sent` carries the state checked.** The wake stores the state it checked (`lookout_wakes.state`); when its message is stored the Lookout calls `rhythm.record(owner, item_id, "sent", at, state=..., topic_keys=...)` with every topic the message covers; the rhythm keeps them on the `sent` event and the 3-hour limit holds each of them. The brief's state is read when it posts. The web's `shown`, `opened` and `acted_on` (including a tap on the message's file card) and topic touches then score that cell.
- **One topics listener.** `register_topic_listener(fn(owner, before, after))` from the promise build (each side a `Topic`, `before` `None` for a new topic) is the only hook; the Lookout's promise listener reads it, and its signals are keyed `promise:<topic>:<id>` with the stored ids.
- **The wake runs through the work queue.** `launch_wake` submits `lookout_wake` jobs keyed by the wake id. The Lookout's handler runs the hidden task through the parent-bound fire path (`helper_fire.run_fire_job`: the same gates, Helper launch and run record) only while the wake's signals are still claimed; a job that fails, or ends with nothing started, releases them. The brief and Automations stay `scheduled_fire` jobs. The fire handler is now registered at startup, so jobs waiting from before a restart are claimed again. Topic watch's kind has no handler and its tasks are deleted by the Lookout's migration. Migrations run in one line: `tt` → `wq` → `rh` → `lk`.
- **A cost cap per wake run.** The hidden task carries the scheduled task's own per-run budget, `max_cost_usd` = 0.50 USD. A parent-bound run sits in its Conversation's spawn tree, so the fire path attaches the sub-agent form of the budget (`subagent_cost_budget`), which counts only the run's own session, never the Conversation's spend (the session-wide form read the whole Conversation and refused the first tool call; an Automation or brief with a cap had the same fault and is fixed by the same change). A run posted by code (the wake, the brief) is `unattended`: never asked; over the cap or on a model with no price it is refused with a plain reason. The Lookout logs each run's spend.
- **A failed run posts nothing.** A run that failed, recorded an error, had a tool call denied by a policy, ran on an unpriced capped model, or whose Result reads as a policy notice posts nothing; its signals wait for the next wake, at most two attempts each, then they are settled `failed` and their topic marked "New". The reason is logged.
- **No wake on an empty tray.** After a launch the next wake is only what still waits; when a run ends the Lookout looks again at the earliest waiting signal, or sets no wake at all.
- **The draft is a card, not a file name.** The file the run saved (the Lookout's draft, the brief's page) is linked to the posted message through 0021's message link with a new `how`, `made`; the transcript shows it as that file's card under the message. The wake prompt now says "the draft" instead of naming the file. Lookout messages are labelled "Topic update" on the web.

**Amended 2026-10-25 after the train run.**

- **A code-posted run has no dispatch id.** The wake, the brief and the quiet-moment pass were still stamped with the dispatch id a runner's restart recovery uses to find Results the Conversation never collected, so after the host restarted the next message brought "1 result waiting in inbox" into the Conversation. Only a run that wakes its parent gets the id now, and recovery skips any ad-hoc run (rows written before this). The Result is the Lookout's code's alone; the raw Result stays in the hidden run's own record.
- **A background run is offered its own tools.** The runner kept only the context-mode labels when it chose a session's tools, so the wake never saw `mail_read_thread` (nor the brief `brief_save`). It now keeps the background-run labels too, and the granted-tool cache tells a background run from an ordinary Helper.
- **Home follows the work.** A topic records when the person last worked on it (`worked_at`: a pick, or the Conversation's own write of its topic). The feature is the newest unseen prepared draft the person has not worked on since it came, else the soonest due or past promise on a topic not worked on today, else none. A tile shows the status line (where the work stands), with "due today" or "was due …" after it when a promise is due or past and it fits. Writers word `status_line` as where the work stands now (what just happened, what is next, at most 80 characters), never a copy of a promise. Home re-reads topics and the brief each time it is shown.

**Seams for the parallel build.**

- `work_queue` (engine infrastructure): `submit(owner, kind, key, payload, not_before=None) -> job_id`, idempotent on `(kind, key)`; `register_handler(kind, fn)`; a global limit and the existing per-owner limit; leases released by the run reconciler.
- `rhythm` (feature folder): `good_moment(owner, at, state) -> bool`, `next_good_slot(owner, after, state) -> datetime`, `record(owner, item_id, event, at)`; cold start and the person's level as the ceiling live inside it.
- `lookout` (feature folder): `add_signal(...)`, `remove_signal(owner, dedupe_key)`; registers for topic writes to keep promise signals in step.
- `topics`: a listener hook called after every topic write with the topic before and after.

**Amended 2026-10-25: what the person says is a rule.** The owner reversed "behaviour only": when the person says how to be reached ("don't bother me with notifications", "only ping me at 9 and 4", "nothing about Polaris this week", "only if Dana writes"), that is taken as is. Learned rhythm stays, but only inside what the person allowed.

- **Reach rules.** The Conversation saves them with one tool (`reach_rules`), only from the person's own words in the chat, never from mail, files or pages. A rule is one of: pause (all unasked messages off, until a date or until lifted), level (0 / 1 / 3 / 6 a day), hours (allowed days and times, or fixed check-in times), only for (named people, topics, or `now` urgency only), mute (a topic or a person). A rule may expire ("today", "this week").
- **Rules beat rhythm.** Rhythm, the exploration floor and `now` urgency all stay inside the rules. "Don't bother me" stops Lookout messages; signals still set the topic's "New" line and wait for the brief.
- **The brief is its own thing.** A pause does not stop the morning brief unless the person names it; the confirmation says so in one line ("Your morning brief still comes at 7:30. Say so and I'll stop it too.").
- **Said back, shown, undone.** The assistant confirms each rule in one plain line. Settings lists the rules as sentences, each with remove, above the learned line; Start over clears what was learned, not the rules. "You can message me again" lifts a pause.
- **Still no ratings.** Thumbs and scores are not inputs; a correction in the chat ("that wasn't urgent") is a rule only when it says what to change.
- Engine: a `reach_rules` table and tool, and `good_moment` / `next_good_slot` read the rules first. Web: the rules in Settings, which waits for a mock-up with the Rhythm line.

**Built 2026-10-25 (reach rules).** Settled in code (`superchat/rhythm/reach.py`): a newer pause, level or hours replaces the older one; a spoken level and spoken hours replace the stored level and window (the newest word), check-in times allow 30 minutes after each, days default to weekdays. The Lookout decides per topic, so it now passes each signal's sender: one from a person named in "only for" lets the topic through, a person mute holds a topic only when all its signals come from that person. Rule mutes stay in `reach_rules` (they can expire and are said back as sentences) and hold signals; *Not useful* mutes stay the outbox's and drop them. A pause that names the brief skips each scheduled brief (never deferred, so no pile of briefs when it ends; Refresh now still runs). Adding or lifting a rule tells the Lookout through `rhythm.register_rules_changed` (wired in `features.py`): with signals waiting, its next wake moves to now; a rule that ends needs no call, since `next_good_slot` already returns its end and held signals wake then. No turn block yet: the Conversation answers "what did I tell you?" with the tool's `list`.

**Amended 2026-10-25 (reads in a background run).** The train run lost two wakes with good Results: a `recall` over an old chat quoted a budget notice, and "a tool call denied by policy" was read from tool output text. Now only the Result decides: a run fails when it left no Result, recorded an error, or its Result reads as a policy notice; a notice in a tool output is logged, kept in the run's record, and the Result is posted (the wake and the brief alike).
A code-posted run is stamped `omnigent.unattended` at creation; the approvals policy denies its classified calls (send, post, delete, spend, upload, share) whatever the standing rules, never asks. Reads (recall, files, topics, `mail_read_thread`, web search and fetch, which the classifier already leaves alone for every Helper) and the run's own draft stay unclassified and allowed.
