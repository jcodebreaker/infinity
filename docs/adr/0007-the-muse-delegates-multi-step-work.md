# The Muse delegates multi-step work to Helpers

> **Status:** Amended by [ADR 0029](0029-a-round-is-recorded-and-the-harness-owns-the-words.md): the step limit and its refusal are gone, and delegation is judgement, a line in the prompt. The rest stands.

The Muse had every work tool (browser, shell, files, web, artifacts) and a soft rule to delegate "substantial work", so it did nearly everything itself in the Conversation: a three-search comparison plus a written page ran as one long Conversation turn, and in its whole history the Muse had chosen to start a Helper twice. We follow the pattern established personal agents use: the Muse keeps all its tools and does single-step work itself, but anything long, multi-step or self-contained goes to a Helper so the Conversation stays responsive. Because the model does not reliably follow that rule from wording alone, the engine backs it with a step limit: a Conversation turn may make 3 work tool calls (search and fetch, browser, shell and files, artifacts); the next one is refused with an instruction to hand the rest to a Helper, along with what was found so far. Coordination tools (memory, cards, Goals, scheduling, vault, Ideas, Helper management) never count. The person saying "do it here" lifts the limit for that turn.

There is one general Helper type, `worker`, with the Muse's full toolset. A `worker` may coordinate other `worker`s, one level deep. When starting a Helper the Muse picks one of two models, `fast` or `strong` (mapped to real models in config, never named in the prompt), plus a reasoning level; the engine rejects anything else. Background passes stay pinned to the fast model.

The host's workspace follows one layout so delegated work lands where the person can find it: `your_files/` for what the person is meant to see, `goals/<goal>/files/` and `goals/<goal>/hidden_files/`, `user/` and `user/media_library/` for what the person hands over, `Downloads/` for the browser, and `/tmp` for scratch. Deliverables are written straight into their home, never copied in afterwards.

## Considered options

- **Take the work tools away from the Muse.** Rejected: it could not even do a one-step check, and every small question would wait on a Helper.
- **Prompt wording only.** Rejected: the existing wording already said to delegate and was ignored; the limit is what makes it hold.
- **Keep specialist Helper types (researcher, analyst, drafter).** Rejected: real tasks span them (research, then write a file), so the Muse picked wrong or did the work itself; one general type plus a coordinator covers fan-out.
- **Let the Muse name any model.** Rejected: no bound on cost or quality; two named choices keep the decision with the Muse and the spend predictable.

## Consequences

- The engine gains a generic per-turn work-step limit for Super Chat turns and a model allowlist for Helper spawns.
- `researcher`, `analyst` and `drafter` retire; followed topics that ran as `researcher` move to `worker`. `goal` stays.
- Results arrive as Activities and through the existing Helper result path; the Muse acknowledges in one line and ends its turn instead of narrating.
