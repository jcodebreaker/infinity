# Act by default, tasks have state, a pick resumes the work

> **Status:** Accepted and built 2026-10-17 (the tasks feature and its migration, the look chooser, the structured card answer end to end, the stall log, the "How a turn ends" prompt section, ten offline scenarios and the live scenario runner); then reworked the same day after review: the task is an explicit record opened at `deck_new`, `start_helper` or a first save, building while its turn or helper runs, done only when that owner finishes (a helper's drafts never close it); one stall rule in `tasks.record` shared by the runner hook and the scenarios; cards carry their task id and answers persist in `card_answers`; a step-limit refusal reaches the record through `Feature.on_tool_denied`. Amends the deck rules of [ADR 0011](0011-a-deck-is-an-html-file-exported-in-the-host.md) (the look gate) and the step limit of [ADR 0007](0007-the-muse-delegates-multi-step-work.md). Builds on [ADR 0022](0022-the-orchestrator-narrates-while-helpers-work.md) and [ADR 0023](0023-the-plan-is-visible-and-the-work-is-alive.md).

"Create some presentation about Acme vs Globex" produced, in four turns: a "Which look?" card; after the pick, "I haven't built it yet, so I'll stop here for now" and a second question; after the answer, "Which look?" again with three other looks; after the second pick, "my last attempt didn't register your choice, send me a short message to continue". Nothing was built. Each turn obeyed a rule.

Three things went wrong, and none is the model's fault alone:

- **Guardrails are refusals.** The look gate (`deck_new` refuses unless the stored items prove the pick), the step limit (a DENY that says "end your turn"), the helper refusal. Each is the engine distrusting the model, and each reaches the person as a stall.
- **The task has no state.** The pick counts only when it is the newest person message with the card right before it. One question in between and it is gone. Nothing holds "deck Acme vs Globex, look Pitch Deck, data mock, next: build", so every turn re-derives the task from the shape of the transcript.
- **Asking is the default.** The deck rules say a new deck always starts with the look question, then list everything that must not settle it: memory, earlier decks, last time. For a casual request the right move is to choose, build, and offer one tap to change.

## Decision

- **Act, confirm by exception.** A deliverable request is started in the same turn. The assistant asks first only when the person asks to be asked, or when a wrong guess would waste real work and no record, memory or topic settles it. A deck's look is chosen by code from the topic and the person's taste (the `TOPICS` map and memory order the themes; the first is the choice); the file card shows the look with "Change look" beside it, and a change is a restyle of the same file. `look_from` stays as a record of where the look came from; the refusal path and the "Which look?" card on a new deck are removed. Restyling an existing deck without a named theme still asks, as today.
- **A task has state, kept by the engine.** One small record per deliverable in progress: kind (`deck`, `page`, `document`, `sheet`, `file`), title, the fields the kind needs (for a deck: look, data source, slide count), what the person has answered, status (`open`, `building`, `done`, `dropped`), and the ids of the helpers and artifacts it produced. The engine writes it from what happened (a tool call, a card answered, a helper started, a save), never from the model's word. The turn prefix carries the open tasks in one line each: `Open task: deck "Acme vs Globex" · look Pitch Deck · data: mock · next: build`. Gates and the model read the same record; the transcript-shape heuristics in `decks/look.py` go.
- **A pick is a structured event, and it resumes the task.** A card answer sends the card id and the option id, not only the label. The engine records the answer on the task and dispatches the turn with the task line first: "They picked Pitch Deck. Continue." The model continues the task; it does not re-plan it. The label still goes in the transcript so the person reads their answer.
- **A turn ends in one of three ways.** A deliverable (a saved file, a card with the answer), a question card, or a started helper. A turn on a deliverable request that ends with none of them is a **stall**: the engine logs it with the session id and the task, and the prompt's "how a turn ends" section is the only place that says when to stop. The step limit still counts; its refusal says "start the helper now", and a turn that then ends without starting one is a stall too.
- **Scenario evals.** Ten scripted conversations ("a deck from a CSV", "a deck with no look named", "pick, then a question, then continue", "a steer mid-helper", ...) run against the bundle. Offline they run the policies and gates with a scripted model and assert the tool sequence and that no turn stalls. With a model key set, the same scripts run end to end nightly and record first-file time, asks per deliverable and stalls. A stall in a scenario fails the run.
- **Prompt diet.** The deck section loses the "nothing else settles it" list and the "card ends your turn" lines. The assistant bundle gains one short "How a turn ends" section and loses the ending rules scattered through it (cards, step limit, narration). Rules the engine now enforces are not restated to the model.

## Where words and decisions come from

The rule for everything above and for what follows it, so the product stays agentic without getting slow:

- **Code decides structure, state and timing.** Grouping, counts, versions, clocks, gates, the task record, what wakes whom. Deterministic, offline-testable, instant.
- **A model writes every sentence a person reads about the work.** The reply, plan steps, progress lines, flags, the hand-back, activity titles and outcomes, step headings. Code may show a placeholder first (the tidied request, a tool's plain-words name) and the model's words replace it, written in the background by the cheapest model that can (the economy model for titles and summaries, the helper itself for its headings).
- **Text for the model is not prose.** Prefix lines, gate results and notices are terse and fixed; nobody reads them but the model.
- **Chrome is code.** Labels like "Getting started", "3 commands · 1 file", "Back to Super Chat" are interface, kept minimal by the repository's own rule.
- **The model decides when it has a view; code decides only when it passed.** A named look wins; `deck_new` chooses only when nothing was said, and says why so the model can change it.
- **A gate detects; it never speaks to the person.** A stall is a log line. A refused call is an instruction to the model.

Follow-up under this rule: a helper that reported no progress still shows a flat tool log; a background economy-model pass writes its headings, stored beside its title, with the code grouping as the placeholder.

## Engine shape

- `superchat/tasks/`: the task record (table `tasks`: owner, conversation, id, kind, title, fields as JSON, status, created and updated times, produced artifact group ids and helper ids), a feature hook that updates it on `deck_new`, `artifact_save`, `start_helper` and card answers, and a `message_prefix` block that writes the open tasks' lines. Deleting a conversation drops its tasks.
- Card answers: `ask_clarification` options gain an `id` (the engine sets one when the model gives none); the web sends `{card_id, option_id, label}` as the answer's structured context beside the message text, the way `open_artifact` rides beside a message today. The engine stores it on the task and in the item's metadata.
- `deck_new`: `look_from` is recorded on the task; when it is `ask` or missing on a new deck, the engine chooses (`decks/look.py` keeps `TOPICS`, `MOODS`, the named and you-choose matchers, and gains `choose_look(topic, memory)`); the card is never returned for a new deck. The result tells the model which look was used and that the card offers "Change look".
- Stall metric: at turn end the runner asks the tasks feature whether an open task existed at turn start and whether the turn saved, asked or started; logs `stall for <session> on task <id>` otherwise. One counter, no behaviour change.
- Scenario evals: `engine/tests/scenarios/` with a scripted harness (the existing policy and tool handlers, a fake model that follows a script) and `engine/scripts/scenarios.py` that runs the same scripts against a live engine when `OMNIGENT_SCENARIO_MODEL_KEY` is set.

## Consequences

- Web: the "Change look" affordance on a deck's file card; the structured card answer; nothing else.
- Engine: the tasks feature, the look chooser, the structured answer, the stall log, the scenarios. The look gate's refusal and card path go; `HELPER_REFUSAL` stays for background runs with no topic.
- Prompt: both bundles re-rendered from the templates.
- Memory: a chosen look the person keeps is not a claim; a look they change twice is worth one.

## Considered options

- **Keep the gate, fix the evidence window.** Rejected: it keeps asking first, and every other gate has the same shape; the record fixes all of them.
- **Let the model keep task state in its working list.** Rejected: the working list is the model's own notes, rewritten at rollover; a gate cannot trust it, and it is not there on a cold start.
- **Make every refusal an engine-side action** (the engine starts the helper when the limit is hit). Rejected for now: it hides a prompt problem behind machinery; the stall metric says whether it is needed.

## Update: categories and evidence (ADR 0027, 2026-10-17)

The task record now keeps the plan and its evidence state, and the decision line of a finished deliverable goes to the working list and the journal. Model picks are three categories, Foundation, Builder and Light; Settings is their source of truth.

## Amendments

**Amended 2026-10-19 (fix wave): the task record is replaced by the Round record ([ADR 0029](0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).** Nothing creates, updates, settles or reads a task any more: no hook on `deck_new`, `artifact_save` or `start_helper`, no `open` / `building` / `done` lifecycle, no plan or evidence state, no runtime stall log. What stays, in `superchat/tasks/`:

- **Open task lines** are written from the Rounds not settled (`rounds.read.open_rounds`): `Open task: "<ask>" · helper "<title>" · next: wait for the helper`, plus the asking Round while its Helper runs, with `now` and `steps done` (`tasks/service.py` `TaskService.view`, `tasks/record.py` `round_line`). The look, data source and slide-count fields are gone from the line.
- **A pick resumes the work.** The structured card answer is kept on the message that carried it (table `card_answers`, `tasks/store.py`), and the turn prefix leads with the pick line; `deck_new` reads it.
- **The stall rule** (`tasks/record.py` `ends_well`) is used only by the scenario runner (`engine/scripts/scenarios.py`), not at turn end.
- **Leftover:** the `tasks` table (`SqlTask` in `db/db_models.py`) is still in the schema and is only deleted from when a Conversation is deleted; nothing writes it.

Act by default, the look chooser (`decks/look.py` `choose_look`, `look_from` as a record) and "Change look" stand as decided above.
