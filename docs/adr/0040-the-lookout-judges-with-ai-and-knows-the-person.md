# 0040: The Lookout judges with AI and knows the person

> **Status:** Accepted 2026-10-25 by the owner ("feel free to make it better without that constraint"). Amends [0038](0038-one-lookout-per-person-wakes-on-signals.md) (the code check, the per-run budget) and [0039](0039-email-and-calendar-are-lookout-sources.md) (code triage decides what is a signal).

0038 kept the Lookout cheap: code decided relevance by word and person matching against topic watch lists (strong, medium, weak), nothing read memory, and each wake run was capped at 0.50 USD. The owner never asked for that trade. It shows: a weak match costs a full run while a mail from a key client about something new is dropped because no topic lists its words, the level never reaches the run that does the work, and drafts are written without knowing who Dana is or how the person writes.

## Decision

**Code only removes what can never matter.** The code filter keeps the hard rules: own mail, no-reply and bounce senders, list and bulk headers, auto-replies, mail not addressed to the person, threads the person answered last, the person's mutes. Word and person matching stay, but only as a hint passed on; they no longer drop anything.

**A Judge decides relevance.** Every signal that survives the filter is judged by a model, batched once per wake, called by the engine directly (like memory upkeep, no tools, no host). The Judge sees:

- the signals (sender, subject, excerpt, thread, the code's match hint and reason);
- the person's open topics (title, where it stands, open lines with dates, watch list);
- a memory pack: the people and organisations the person deals with and what they are to them, standing priorities and preferences, from memory claims;
- the person's own words in the last day of the Conversation.

For each signal it returns `relevance` (0–100), `topic` (an existing key, or none), `urgency` (`now`, `today`, `later`), and a one-line `why`. It runs on the person's strongest model (the model their Conversation runs).

**Timing uses the Judge.** Below 40 a signal never starts a message; it waits for the morning brief. `now` skips only the learned timing (the person's rules, hours and daily limit still hold, 0038). Everything else as in 0038.

**The work run gets everything.** The wake prompt carries each signal with its relevance, urgency and why, the touched topics, and the same memory pack plus the person's writing voice (how they open, close and sign mail, from memory and their sent mail when connected). It is told to read the full thread and files before drafting.

**No per-run cost cap.** The 0.50 USD budget on the wake and the brief is removed. Runs keep the normal step and time limits of any run, and the unattended policy (read and draft only) is unchanged. Cost is visible per wake in the run record and the model-call logs.

## Considered options

- **Keep code-only relevance, add memory terms to the watch lists.** Cheaper, still blind to anything phrased differently.
- **Let the work run judge relevance itself.** One call fewer, but timing has to be decided before that run, and a judge without tools is fast enough to run on every wake.
- **Judge with a light model.** Relevance is the whole product here; the owner asked for the better result.

## Consequences

- Engine: a Judge step in the Lookout (batched call, prompt and parser in `superchat/lookout/`), a memory pack built from memory claims, the person's recent words, the relevance threshold and `now` handling in the wake, the wake prompt gains scores and the pack, the cost cap removed from the wake and brief tasks. No host change.
- One more model call per wake that has signals; none on a quiet day. Logged with the wake.
- 0038's strong/medium/weak become hints; 0039's "code decides what is a signal" holds only for the hard filter.

## Built 2026-10-25

Settled in code:

- **Where it runs.** `superchat/lookout/judge.py` holds the prompt, the input (signals as `s1`, `s2`..., topics with their open lines and dates and watch lists, the memory pack, the person's recent words, each fenced as data) and the strict parser. The wake calls it after the hard filter and before the rhythm; only signals without a verdict are judged, so a held signal costs no second call, and a newer mail in a thread (which replaces the signal's detail) is judged again. A flood is judged 25 at a time. One log line per wake names the model, whether memory and context were there, and each signal's score, urgency, topic, source (`model` or `hint`) and why.
- **The model.** The model the person's Conversation runs, resolved as a turn resolves it (`session_model`: its pin, else their Foundation), called over that Conversation's own connection (`resolve_session_llm`). Without one, their Builder pick on the background harness (`resolve_background_llm(owner, category="builder")`), then their Light model. The wake run and the brief run the same model: their tasks store the model `conversation` (`CONVERSATION_MODEL`), which the fire path resolves the same way, falling back to Builder; tasks made with `builder` move over when next ensured. The server's `llm:` block, when set, still wins for all background work. No model: every signal takes the fallback.
- **Fallback.** From the code's hint: strong 70, medium 55, weak 40, none 20, urgency `later`. A source that names the topic itself (a promise) counts as strong; a mail thread the person wrote in with no topic counts as medium, so a mail code always ran keeps running when the Judge is down.
- **The hint.** Mail triage and the wake keep the match as `match_strength` and `match_reason` on the signal; an unmatched signal is matched once (`no topic match`), so the term counts are not inflated by a held signal. Mail no longer drops "unrelated to any open topic".
- **Mutes after the Judge.** A topic the Judge gives a signal gets the person's *Not useful* mutes like a code match (the whole topic, or the sender on it).
- **The brief.** It claims what waited below the floor along with everything else. A low signal the brief never claims wakes the Lookout when it goes stale, which only marks its topic "New".
- **Memory pack and voice.** `omnigent/memory/pack.py`: `memory_pack` is the profile plus up to 40 `person` claims; the brief now reads it too. Both read the runtime's memory service, which the server registers at startup (`set_memory_service`). `writing_voice` is the person's working style plus their preferences and instructions about mail, replies, tone or sign-offs; when memory knows nothing the wake prompt says so and asks the run to match the person's own messages in the thread. The person's sent mail is not read yet (no provider gives it).
- **Conversation context.** The person's own messages in their Conversation over the last day (runtime notices left out), newest first up to 2,000 characters from the last 60 messages, shown oldest first. Not the fold's summary: the fold runs only when the context fills, so a whole day can pass without a checkpoint.
- **No cap.** The wake task is made without `max_cost_usd`, and an existing one is re-ensured with the cap cleared; the brief never had one. Each run's spend is logged.


## Amended 2026-10-25 (first live wake)

The first live wake judged with no memory and no context, on the Builder. The server never registered its memory service in the runtime, so every in-process reader of `get_memory_service()` got nothing; the fold had written no checkpoint all day; the owner wants the strongest model. The bullets above are the fix.
