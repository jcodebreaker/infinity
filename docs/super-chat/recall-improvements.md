# Recall: improvement candidates from the literature, mapped to our design

Status: research notes, 2026-10-15, written before the review of the Recall implementation. Each candidate names
its evidence, what it would be in our architecture (`recall.md`), its cost, and a verdict. Nothing here is built.

## 1. What the evidence says, in one paragraph each

**LongMemEval** (Wu et al., ICLR 2025): long-context models lose 30–60 % against oracle evidence on long chat
histories; commercial assistants lose more, and ChatGPT "tended to overwrite crucial information as the chat
continues". Five abilities: extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention.
Design levers that measurably helped: index at **round** granularity (one user turn plus its reply) rather than
whole sessions; **fact-augmented keys** (index the raw text plus a few extracted facts as extra key text: +9.4 %
recall, +5.4 % accuracy averaged over models); **time-aware query expansion** (extract a time range from the
question and filter the index by it: +11.3 % temporal recall at round granularity); **structured reading** (JSON
items plus a chain-of-note pass beat prose). Replacing values with summaries hurt; facts as keys helped.

**Context rot** (Chroma, July 2025, 18 models): accuracy falls with input length even on trivial tasks; one
distractor already hurts and distractors compound; low similarity between question and evidence degrades faster;
focused prompts (≈ 300 tokens) beat full ones (≈ 113k) on LongMemEval for every model, with Claude's gap driven by
abstention under ambiguity. Takeaways: send focused context, cut topically close distractors, test at real lengths.

**Sleep-time compute** (Letta, April 2025): a background agent, not the primary one, owns memory editing and turns
"raw context" into "learned context" while the system is idle; the primary agent reads memory any time and never
waits. Frequency is a token-cost knob.

**Generative agents** (Park et al., 2023; 2026 survey arXiv 2603.07670): retrieval that blends recency (exponential
decay), relevance (embedding) and **importance** (a model-assigned integer per memory) beats plain similarity;
periodic **reflection** clusters raw observations into higher-order notes.

**Claude Code auto memory** (v2.1.59, Feb 2026): a small index file is loaded at start (a line and size cap),
topic files are read on demand with ordinary file tools, and a consolidation step merges notes. Claude.ai Projects
memory updates on a nightly cycle. This is the shape our `STATE.md` (loaded) plus journal (on demand) already has.

## 2. Verdicts, kept small

Rule applied: take a candidate only when it is a field, a flag or an instruction line on something already being
built, and it addresses a failure the evidence shows. Anything that adds a pass, a score or a schema waits for the
evaluation to prove the need.

### Take now (fold into the review)

| # | Candidate | Why it is cheap | Why it makes sense |
|---|-----------|-----------------|--------------------|
| 1 | **Round granularity** for the conversation source | already the plan (`recall.md` 3.1): one passage per turn, written at compaction from the retired window, never per turn on the hot path; the review checks both | LongMemEval: rounds beat sessions; no concurrency with live turns |
| 2 | **Planner returns `since` / `until` and `historical`** | two fields on a schema the planner already fills; one filter the search already has (`since` in 8.3) | +11 % temporal recall; temporal questions are among the hardest |
| 3 | **Strict hint floor** | a threshold constant that exists | one distractor already hurts; focused beats full for every model |
| 4 | **Hand edits become journal lines** | one more caller of the journal writer that already exists for Helper results | the "since we last spoke" block and per-object history otherwise miss the person's own work |
| 5 | **Five-type evaluation** | the paid 20-probe run is already planned; this only picks the twenty questions: four each of extraction, multi-session, temporal, knowledge update, abstention | tests the abilities the benchmark says fail, instead of twenty random probes |
| 6 | **One instruction line** | `AGENTS.md` | when hits conflict and the working list does not settle it, ask one question naming the dates; never guess (Claude abstains under ambiguity; make the abstention useful) |

### Not now

| Candidate | Why not | Would reconsider when |
|-----------|---------|------------------------|
| Fact-augmented keys from journal lines | touches the indexer schema and re-embeds turns at compaction; real work for a gain measured on another system | the evaluation shows paraphrased probes miss while exact ones hit |
| Importance score per journal line | a number nobody consumes yet; tuning risk | ranking by recency and relevance is shown insufficient |
| Structured hit objects with a chain-of-note clause | hits are already `{date, source, origin, text, id}`; the extra instruction adds words to answers | never, as a separate item; it is the existing shape |
| Nightly consolidation and weekly roll-up files | a new background pass with a model call | old-period hints are measurably thin (many weak lines, no dense one) |
| Lower rollover threshold | a quality knob with no evidence on our models | the year test or the evaluation shows better answers at 120k |
| Project as a hard filter | re-ranking by Project already comes from refs | wrong-Project hits show up in the evaluation |
| Knowledge graph, model-extracted memories, local models | wrong shape for us (see 3) | not planned |

## 3. Not taken, and why

- **Knowledge graph (Zep/Graphiti style):** our refs (8.2) are the edges we need (object ↔ events); a graph database
  answers questions nobody has asked.
- **LLM-extracted memories from assistant text (mem0 style):** would weaken the user-words-only rule that keeps
  standing memory clean. The journal is lookup, not belief; keep it so.
- **Summaries as stored values:** LongMemEval found replacing values with summaries loses information. We keep raw
  turns as values and use summaries only for the state file.
- **Local GGUF models for expansion or rerank (qmd):** we have the model proxy; no models in the Computer image.

## 4. Order for the review

Items 1–4 and 6 are checked or added during the review of slices 5a/5b. Item 5 shapes the evaluation. Nothing in
"Not now" is started without the trigger in its row.

## 5. Sources

- LongMemEval: https://arxiv.org/html/2410.10813v2
- Chroma, Context Rot: https://www.trychroma.com/research/context-rot
- Letta, Sleep-time compute: https://letta.com/blog/sleep-time-compute ; docs: https://docs.letta.com/guides/agents/architectures/sleeptime/index.md
- Generative Agents: https://arxiv.org/pdf/2304.03442 ; 2026 memory survey: https://arxiv.org/pdf/2603.07670
- Claude Code auto memory (secondary): https://www.engineerscodex.com/claude-code-auto-memory , https://claudefa.st/blog/guide/mechanics/auto-memory
- Anthropic, Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
