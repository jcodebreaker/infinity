# Glossary

One word per thing, as of 2026-10-19. The infrastructure words come from
[ADR 0018](adr/0018-one-vocabulary-engine-host-runner-harness.md); a pull request that introduces a
second word for one of these is sent back. Deprecated words are marked here with what replaced
them; where code still uses one, the entry says so.

- **Activity**: one piece of multi-step work in the activity panel, either a conversation turn that did real work or a whole helper task; derived on read, never stored ([helpers.md](helpers.md#the-activity-panel)); a projection of the record, its rows grouped by Round ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).
- **Archived**: a session hidden from the default list (`PATCH /v1/sessions/{id}` with `archived: true`); nothing archives on its own.
- **Artifact**: a file the assistant saved with `artifact_save`; saving the same name again in the same conversation adds a version ([artifacts.md](artifacts.md)).
- **Ask**: anything the assistant is waiting on the person for (an approval, a question); open asks are listed by `GET /v1/me/asks`. Not the unit of work: that is a **Round**.
- **Assistant**: the one persistent agent a person has; it owns the conversation, the memory and the host. The canonical word in copy, docs and prompts ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)). Retired words, kept here only: Muse, "Super Chat" and Nova. Code identifiers still say Muse (`/v1/me/muse`, `superchat/muse.py`, `muse_*` names) and super chat until the naming PR of the ADR 0029 train; the stored label value `omnigent.context.mode=superside-chat`, carried by the Conversation and its helpers, stays an identifier.
- **Brief**: the task a helper starts from (`start_helper`'s `task`), with engine facts appended: the folder, the files in play and the preference lines that apply, never the whole profile ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)); the helper does not see the conversation.
- **Checklist**: the model's own list of steps, words only: Omnigent's `checklist` tool on both harnesses (Claude's native task tools are disallowed), folded to one `checklist.*` event. Never bound to a Helper; it drives no state, refusal or tick ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).
- **Checkpoint**: the `compaction` item a rollover stores: a fixed header written by code, the working list, and the kept tail.
- **Chip**: the pointer on a message to the artifact, or part of one, the message is about ([conversation.md](conversation.md)).
- **Claim**: one sentence about the person in long-term memory, with its quote, evidence, kind and confidence (table `memory_claims`).
- **Computer**: old name for **host**, gone from code, config and copy since the rename wave (ADR 0018). The person's word for their host is "your machine"; the web tab is Files.
- **Context brief**: the engine's account, built when the assistant calls `context_brief`, of what the person made before on the subject, what earlier turns and the journal say, standing preferences, open tasks and matching sources ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md)). Pulled by judgement, never ordered first ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).
- **Session panel**: removed 2026-10-25 ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md) amendment). Files a reply made are in Files; its steps are in Agent Activity and the work strip.
- **Paused**: a helper state: held at its next tool boundary until resumed; cancelled after thirty minutes unresumed.
- **Conversation**: the one chat between a person and the assistant; code still uses a retired name for it (see **Assistant**).
- **Daily note**: one note per person per local day, kept by background `notes` helper passes; the person's own edits are protected ([context.md](context.md#daily-notes-and-the-working-list)).
- **Deck**: an HTML artifact named `*.deck.html` with 1920x1080 slides, exported to PowerPoint or PDF in the host ([decks.md](decks.md)).
- **Foundation**, **Builder** and **Light**: a person's three model picks per harness, chosen from the catalog by capability and price and overridable in Settings ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md)). Picked automatically, Foundation is the default the connection's settings name, else the dearest priced model, Light the cheapest and Builder the middle one (Foundation's when fewer than three are priced); with nothing priced all three are the connection's default. The Assistant runs on Foundation; helpers on the category the assistant names, Builder by default; titles, summaries and rerank on Light. Replaces Default and Light; the `fast` and `strong` helper picks are aliases for Light and Foundation for one release.
- **Eval set**: moved to evalution with the rest of evals ([ADR 0043](adr/0043-evals-move-to-their-own-project.md)). Superchat keeps only **eval persons**, `eval+<run>@<eval domain>` ([evals.md](evals.md)).
- **Engine**: the server, one process: the API, the built web, Postgres, sessions, scheduling and the model proxy.
- **Feature**: a folder under `engine/omnigent/superchat/` with a `FEATURE`, registered once in `superchat/features.py`.
- **Fork**: deprecated and removed ([ADR 0021](adr/0021-one-conversation-messages-link-to-artifacts.md)); a message links to an artifact instead.
- **Grader**: an evalution term since [ADR 0043](adr/0043-evals-move-to-their-own-project.md). Not the Lookout's **Judge**.
- **Harness**: the model loop inside a runner, `claude-sdk` or `pi`. Replaces executor, inner, native.
- **Helper**: a child session the assistant starts with `start_helper` for one bounded job; read-only to the person, its result goes back to the chat that started it ([helpers.md](helpers.md)). Replaces sub-agent.
- **Helper type**: what a helper is configured as: `worker`, `subworker` or `notes` (`engine/agents/templates/agents/`).
- **Host**: a person's execution environment, today a Docker container the engine starts (`superchat-host-<key>`, labelled `omnigent.host.owner`); runs `omnigent host`. Replaces Computer, sandbox (as a provider), managed host. In copy it is "your machine".
- **Host provider**: how a host is launched; `docker` today (`engine/omnigent/hosts/docker.py`), set by the server config's `hosts:` block and `OMNIGENT_HOST_PROVIDER`.
- **Journal**: dated lines under `~/workspace/memory/journal/` in the host, written by code from checkpoints, hand edits and one entry per settled Round ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)); a copy for search, never read back as state.
- **Kept tail**: the most recent whole turns a checkpoint keeps word for word (up to 16,000 tokens).
- **Judge**: the one model call per Lookout wake that scores each signal for the person (relevance 0-100, topic, urgency, why) from their topics and memory pack; below 40 a signal waits for the morning brief ([ADR 0040](adr/0040-the-lookout-judges-with-ai-and-knows-the-person.md)). Avoid classifier, triage model.
- **Knowledge index**: the search index in the host (SQLite with FTS5 and sqlite-vec) over the person's files, the journal and retired conversation turns; behind the `files_*` tools and `recall`.
- **Launch token**: the per-launch credential a host uses for its tunnel and its harness uses at the model proxy; the engine stores only its digest.
- **Live page**: an HTML artifact kept up to date from source files by scheduled helper runs; a **live sheet** is the same for CSV and XLSX cells ([artifacts.md](artifacts.md#live-pages)).
- **Lookout**: the one per-person part of the assistant that decides whether something is worth telling the person unasked, and when; it owns everything that can message the person on its own ([ADR 0038](adr/0038-one-lookout-per-person-wakes-on-signals.md)). Avoid desk, attention loop.
- **Memory profile**: the block of high-confidence claims put in front of every turn; a brief carries only its lines that apply ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).
- **Memory pack**: the memory profile plus the people and organisations the person deals with, read by the brief, the Judge and the Lookout's run ([ADR 0040](adr/0040-the-lookout-judges-with-ai-and-knows-the-person.md)).
- **Project**: a folder under `~/workspace/projects/<slug>/` with a `PROJECT.md` card; the assistant opens one with `open_project`.
- **Recall**: finding earlier work over one index: the `recall` tool when the model asks, and the optional per-turn recall hint ([ADR 0016](adr/0016-recall-is-a-bounded-hint-and-a-deep-pull-over-one-index.md)).
- **Result**: a helper's final message, posted to the chat that started it as a system notice.
- **Rollover**: replacing older turns with a checkpoint when the conversation grows large or the person returns after a long break.
- **Rhythm**: what the assistant has learned about when a person likes to hear from it unasked, from how they behave after its messages, never from ratings ([ADR 0038](adr/0038-one-lookout-per-person-wakes-on-signals.md)). Avoid scorecard, timing profile.
- **Round**: one message from the person through its settlement: no turn running, no Helper running, no result unconsumed. Assigned at write time; every item, Helper session and artifact carries its `round_id`, and the view is a pure fold of its events. A steer joins the running Round, a wake turn belongs to the Round that started the Helper, and scheduled and system runs get hidden Rounds of their own. Stop stops one whole Round ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)). A Round has **delivered** (`delivered_at`) once its answer was shown (a terminal card, the chips or a question) with no Helper of it running or unread; the strip settles then, the Round at its turn's end.
- **Runner**: the per-session process a host launches; runs one harness.
- **Sandbox**: only the OS process jail (bubblewrap). "Sandbox provider" and the server config's `sandbox:` block are old names for the host provider and its `hosts:` block. Some code still says sandbox for the host (`sandbox_id`, `sandbox_provider`, `sandbox_status`, `managed_sandbox_reaper`).
- **Side chat**: deprecated and removed ([ADR 0021](adr/0021-one-conversation-messages-link-to-artifacts.md)); there is one conversation.
- **Signal**: one thing that happened and might be worth telling the person (a new file, a promise coming due), left for their Lookout without any model run ([ADR 0035](adr/0035-topics-are-written-by-the-quiet-moment-pass.md), [ADR 0038](adr/0038-one-lookout-per-person-wakes-on-signals.md)). Avoid event, which is a dated line on a topic.
- **Steer**: a message sent while a turn runs, delivered into that turn instead of starting a new one.
- **Step limit**: retired by [ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md). It refused a conversation turn's fourth work tool call ([ADR 0007](adr/0007-the-muse-delegates-multi-step-work.md)); delegation is now judgement, a line in the prompt.
- **Sub-agent**: deprecated, now **helper**; still the engine's word in code.
- **Superchat**: the product.
- **Tenant**: the organisation a request belongs to, sent by a sign-in broker in `X-Omnigent-Tenant`; one conversation per person per tenant.
- **Topic status**: one short sentence under a topic on Home saying what needs the person or that it is done ("3 slides need your eye", "Done. Nothing open") ([ADR 0035](adr/0035-topics-are-written-by-the-quiet-moment-pass.md)). Not a topic's open or closed state.
- **Topic tag**: the one- or two-word name a topic carries on Home and in the morning panel, shown in the topic's own colour ([ADR 0035](adr/0035-topics-are-written-by-the-quiet-moment-pass.md)).
- **Upkeep**: the background job that extracts claims from what the person wrote.
- **Vault**: saved website logins, sealed in the engine; `vault_request_secret` asks for one, `vault_fill` types it into the host's browser.
- **Wake notice**: the system notice a helper posts to its parent when it finishes; `progress` and `flag` notices are added by [ADR 0022](adr/0022-the-orchestrator-narrates-while-helpers-work.md).
- **Work queue**: the one line every background model run waits in, across all people, with a limit on how many run at once and a smaller one per person; kept in Postgres so a run survives a restart and never runs twice ([ADR 0038](adr/0038-one-lookout-per-person-wakes-on-signals.md)). Not "waiting line", which is a helper's newest progress line.
- **Working list**: the tagged lines of the checkpoint (`t-` topic, `d-` decision, `o-` open loop, `p-` preference), copied to `~/workspace/memory/STATE.md`; code calls it the state file.
- **Workspace**: the session's working directory, `/home/superchat/workspace` in the host.
