# 0042: An evals agent runs the pipeline, in isolated sessions

> **Status:** Accepted 2026-11-01 by the owner ("I want the evaluation become full agentic, agent setup the test pipeline and review the traces and the results for us, it analyze the context assembly it analyze the agent trajectory, and every run is isolated session so we dont polute the main app"), after the mock-up (`v5`: describe a test, live run, results, what went wrong, the agent fixes its case). Amends [0041](0041-evals-are-cases-in-the-repo-and-runs-are-traces.md).

0041 made evals cases in the repo, played on the live stack and graded by code plus one Grader call, with an analysis after each run. Two things are still wrong:

- **People write cases by hand.** A real question, like "does compaction keep early decisions" or "does memory recall a name after 20 steps", needs long, multi-step cases with checks on engine state. Nobody should hand-write that JSON.
- **Tries share one eval person.** All of them play in the same Conversation, host and memory, wiped between tries. A missed wipe leaks one try into the next (the "Lisbon deck from yesterday" seen live), and the eval person's history keeps growing beside the real app.

## Decision

**An evals agent works for the admin.** The admin describes what to test in plain words, and the agent:

- reads how the engine works (docs, the existing cases);
- writes the case or set;
- shows the plan and the cost;
- runs it;
- reviews the results and the traces;
- reports what went wrong, and why, in plain words;
- fixes its own case when the fault is in the case.

Its tools are the eval API (list, draft, save, estimate, run, read a run and its tries) plus read-only views of a try's trace, context assembly and trajectory. It never edits the Assistant's prompt or code. That stays Evolve's job, behind approval.

**It acts within a budget.** Each agent session has a cap the admin sets (default 10 USD). Within it, the agent starts runs without asking, and it asks before going over. Every run still has an estimate, and the session shows what it has spent. The admin can stop it at any time.

**Every run is isolated.** A run gets its own eval person: a fresh identity (`eval+<run>@<eval domain>`) with its own Conversation, host and memory, made when the run starts. Tries of the same case reset that person's Conversation and clean its files, as in 0041. A different case or run never shares anything. Nothing an eval does reaches a real person's Conversation, Home, Lookout, memory or files.

- When the run ends, its host is stopped and removed, and its home is kept for review for 7 days, then deleted with the identity.
- Eval identities never appear in the admin's people lists, never get proactive messages, and are excluded from the Lookout and the daily brief.

**Cases can test long-running behaviour.**

- **Generated turns:** `{"filler": N, "about": "…"}`. N ordinary messages on a topic, written fresh each try by the Grader model, which push a conversation toward rollover.
- **State checks:** `{"expect_state": {...}}`, checked after a turn:
  - a checkpoint was written
  - memory holds a claim containing a text
  - a file exists
  - the context sent on the next model call contains or lacks a text
- **Context checks** read the context assembly below.

**Every try keeps its context assembly and its trajectory.**

- **Context assembly:** for each model call, the sections of what the model was sent, with their sizes in tokens:
  - the system prompt
  - the memory profile
  - the context brief
  - the checkpoint and kept tail
  - the tool list
  - the turns
  - injected notices

  It comes from the model-call proxy, which already sees each request; content stays dev- and admin-only, as in 0041.
- **Trajectory:** the turn-by-turn list of what the Assistant decided, called and said, with the Round's Helpers and wake turns.

The agent's review reads both. For example: "the checkpoint carried the three decisions; the reply used them without recall", or "the memory profile was 9k tokens of which none was about the client".

## Considered options

- **Keep authoring by hand, with a better form.** It still can't express a long case, and someone still has to read every trace.
- **One shared eval person, wiped better.** Cheaper, but every missed wipe is a wrong result, and the person's state grows forever.
- **A fresh Conversation per try for the same person.** A person has one Conversation, and memory and files are per person, so this isn't isolation.
- **The agent may also edit the prompt.** Folded into Evolve instead, where a change needs zero regressions and approval.

## Consequences

- **Engine:**
  - run-scoped eval identities with host lifecycle and cleanup;
  - excluding eval identities from people-facing features;
  - filler turns and state checks in cases;
  - context assembly sections from the proxy record, stored per try;
  - the evals agent: a tool loop on the admin's strongest model, sessions with budget and spend, routes, and streamed messages to the page.
- **Web:** the v5 mock-up. Describe a test, the agent's thread with its plan and cost, the live run card, results in plain words, what went wrong, and a try's context assembly view.
- **Cost:** long cases cost more (a compaction case is about $1.80 per try). The budget is the guard.

## Amended 2026-11-01 (first live use)

The owner, after the agent ran a case on its own: "the agent create but still user can ask for clarifications or updates", and "cant run directly, we have to add the test to test bucket then user run it". So:

- **The agent never runs anything.** It proposes a case and its cost, answers questions and makes changes, and adds the case to the set only after the admin approves. The admin runs tests from the page ("Run it" on the added case, or as usual).
- **After a run, the admin can hand it to the agent** ("Ask the agent about this run") for a review of results, traces, context assembly and trajectory.
- **The budget now covers only the agent's own model calls.** Run spend is the admin's, confirmed at each run as before.
- **The admin can write to the agent while it works.** The message is read at its next step.
- **The 7-day sweep only deletes eval persons a run document names,** never an address that merely matches the pattern.
