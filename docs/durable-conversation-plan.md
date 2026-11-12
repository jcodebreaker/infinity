# Durable Conversation for pi on Omnigent: technical plan

Status: approved 2026-11-11 and built on the `durable-conversation` branch; the decision is recorded in [ADR 0044](adr/0044-the-record-is-the-window.md).
- **Review record (Fable, adversarial, read-only, 5 rounds):** v1 5 → v2 7 → v3 8 → v4 8.5 → **v5 9/10**.
  - Final round's scores: correctness 9, quality risk 9, implementability 9, simplicity 7.5. Simplicity is capped while the Claude path stays.
  - This version applies the four wording fixes from round 5, which the reviewer said clear every correctness issue it found. They weren't re-reviewed (round cap).
- **Scope:** the Assistant's Conversation on the pi harness. Helpers and the Claude harness keep today's paths except where stated.
- **Paths and versions:** `eo/` = `engine/omnigent/`. pi 0.87.1 (`deployment/host.Dockerfile:51`).
- **Reference product:** a hosted personal assistant with a durable conversation ("the reference" below), inspected on 2026-11-11 through its own agent's self-inspection.

---

## 1. The problem in one page

**What the person feels**
- **The first word after a restart is slow and forgetful.** 17 of 20 cold starts are restarts (ADR 0033). Each one loses pi's verbatim window and rebuilds a ~24k summary-plus-tail from the checkpoint, and pi gets that history flattened into one text prompt (`eo/harnesses/pi_executor.py:1538-1576`).
- **Memory bookkeeping sits in front of the answer.** AGENTS.md says "`memory_search` first … save before you reply" (`engine/agents/bundles/superchat-pi/AGENTS.md:121,127`).
- **Exact old details are hard to get back.** `session_history` returns 300 chars of any tool result (`eo/tools/builtins/session_history.py:25`).

**What the code carries**
- **Eight background processes** touch memory or context (Appendix A): the fold, upkeep with an hourly sweep, five quiet-moment passes, a STATE.md/journal mirror, two turn indexers, the knowledge indexer, plus readers.
- **Five writers** to `memory_claims`.
- **Three search paths** over the past.
- **The per-turn prefix is sent and thrown away** (`eo/runner/app.py:1115-1119`; `eo/superchat/prompt_prefix.py:505-519`). The record doesn't hold what the model saw, so no window can be rebuilt exactly.
- **The profile delta is all-or-nothing:** one changed claim re-sends ≤6 KB (`prompt_prefix.py:113-129`).

**What the reference shows works, and what it gets wrong**

| Reference | Take it? |
|---|---|
| One durable session log; context rebuilt from storage each turn; survives machine swaps | **Yes.** Our record becomes the window (§3) |
| 20k verbatim tail, rolling summary with fixed sections incl. "pending, not delivered" and "open commitments" | **Yes.** Tail size by sweep (§3.6); sections in our checkpoint (§5.3) |
| Compaction after the reply, never blocking | **Already ours** (background fold; turn-start render) |
| Small always-on memory file + long dated trail + claims with supersession, line-level citations | **Already ours** (profile, daily notes, claims); add a cap (§4.1) |
| One memory job, watermark cursor, line edits, no-op when current | **Already ours** (upkeep watermark); drop the redundant clock (§4.3) |
| memory_search on every turn by rule; saves before the reply | **No.** Pull on demand; explicit-only in-turn saves (§4.2) |
| Uncapped tool output; 20 KB proactive blocks in the window; 38% of the summary is a delivery log | **No.** Caps with exact pointers (§3.5); only delivered text enters (§5) |
| No cache observed; 3.15M input tokens for one answer; MEMORY.md has no cap | **Do better.** Cache-preserving fast path (§3.3); profile cap (§4.1) |

**Off-the-shelf pi packages.** About 50 pi memory packages exist (pi-memory, pi-hermes-memory, pi-observational-memory, @amaster.ai/pi-memory, pi-session-memory, …).
- **Why adopt none:** all are single-user coding-agent memory under `~/.pi/agent`, pi-only, and some are cloud-backed. None fits an engine-owned, multi-tenant, provider-neutral record. Third-party extension code in the host is also a supply-chain risk.
- **Ideas borrowed:**
  - source ids that lead back to evidence (our pointers, §3.5);
  - memory prepared ahead of time, so a window swap is a render (our fold already does this);
  - curated always-on memory with a hard cap (§4.1).

---

## 2. Principles

1. **The record is the window.** Everything a harness sends as history is stored in or derived from `conversation_items`. A harness's own copy is a disposable cache.
2. **One renderer.** pi first. Claude keeps today's path until its adapter is proven (§3.7). The plan does not claim one code path before then.
3. **Two kinds of state:** the record, and long-term memory (`memory_claims` + one search index). Daily notes and topics stay as product surfaces. No new memory types.
4. **Nothing between the person and the first word:** no searches or saves by rule.
5. **Every byte that leaves the window comes back by an exact read.**
6. **Never cost more than today.** Every phase has a cost gate next to its latency and quality gates.
7. **pi compaction stays off.** It cuts mid-Round after any tool batch, and a failed hook falls back to pi's own summarizer. The engine keeps the turn-start, size-only window swap (owner decision 4, ADR 0031).

---

## 3. Short-term memory: the window

### 3.1 Record what the model saw

**`turn_context`: a field, not an item**
- **Field:** add `context_prefix: list[str] | None` to `MessageData` (`eo/entities/conversation.py`). Use the existing optional-flag pattern with `exclude_if` (`conversation.py:312-313`), so old payloads are unchanged.
- **Which item carries it:** the user-role item that opens the turn. That is the person's message, a runtime notice or a scheduled wake, because `_prepend_turn_blocks` runs on every turn body (`eo/runner/app.py:1150-1178`).
- **Values:**
  - `None` = not written (unknown);
  - `[]` = written, no blocks were sent.

  Only `None` makes an epoch not exactly renderable.
- **Hidden from clients:** `to_api_dict` spreads data fields into the items API, SSE and the `check_task` tool (`conversation.py:957-974`). Both new fields are excluded from that dump. A test asserts the items API and SSE never carry them.
- **Readers:** the renderer only. The search text extractor, transcript, upkeep, fold, Rounds and export skip the fields.

**The one new write path**
- A runner-credential-only endpoint `PATCH /v1/sessions/{id}/items/{item_id}/data`, plus a store method `update_item_data(item_id, fields)`. It merges only allow-listed fields (`context_prefix`, `delivered_after_call_id`) and is idempotent by item id.
- Today there is no item-update route; only session and resource PATCH (`eo/server/routes/sessions/routes_core.py:2132`, `routes_resources.py:2815`). This is the only new engine surface in phase 3.
- **Caller:** the runner calls it asynchronously after the first output token, off the answer path.
- **On failure:** the field stays `None`, and §3.3 handles it.

**`epoch_block`: one new hidden item type**
- **When and how:** whenever the view start moves (a size handoff, a reset, or the new-epoch branch of §3.3), the runner stores the rendered epoch block as an `epoch_block` item. The runner already builds the block, and `_note_view_start` already patches the view-start label (`eo/runner/app.py:4764-4790`).
- **Position:** items append in time order, so the block can't sit "at" the view start. Instead, a second session label `epoch_block_item_id` is written in the same session PATCH as the view-start label (that label is a single item-id string, `eo/context/rollover.py:503`), and the renderer fetches the block by id.
- **Accepted from the runner's credential only:** `_ALLOWED_EVENT_TYPES` (`eo/server/routes/_sessions/common.py:283`) is one set for all callers, so the route checks the caller for this type.
- **Why not re-derive it:** its inputs (checkpoint, Artifacts list, Projects…) are not all pinned by a checkpoint id plus an anchor.
- **Why not reuse `compaction`:** a stored compaction triggers upkeep and indexing (`eo/server/memory_upkeep.py:6-9`, `eo/runner/memory/sync.py:215`).
- **Registry and filter changes** (from the review's code trace):

| Place | Change |
|---|---|
| `eo/entities/conversation.py:810-822` `ITEM_TYPE_TO_DATA_CLS` | add `EpochBlockData` |
| `eo/entities/conversation.py:827-836` `NON_CONTENT_ITEM_TYPES` | add `epoch_block` (the Claude path and workflow loader must not replay it, `eo/runtime/workflow.py:1366-1419`) |
| `eo/db/enum_codecs.py:136-145` | new code (parity test) |
| `eo/db/utils.py` `extract_search_text` | return "" for `epoch_block` |
| `eo/server/routes/_sessions/common.py:283`, `helpers.py:2586` | accepted from the runner credential only; excluded from the items API and SSE |
| session labels | add a second label `epoch_block_item_id`, written with the view-start label |
| `eo/superchat/admin/user_data.py` | include in export and delete |
| Item-count windows (`memory/upkeep.py:52` 500 items; `superchat/rollover.py:181` page 1,000) | +1 item per epoch, negligible; note in docs |

**Steer delivery: a field**
- Add `delivered_after_call_id: str | None` to a steer's message item.
- **Value:** the `call_id` of the last tool result pi had before it accepted the steer.
  - The runner always knows that call_id. It doesn't hold engine item ids for streamed tool results; it only knows `persisted_item_id` for the person message (`runner/app.py:5402`).
  - If the steer came before any tool result, the value is `"turn_start"`.
- **Set** when `_steer_outcomes_for` reports `taken=True` on pi's user `message_start` (`eo/harnesses/pi_executor.py:2100-2108`), through the write path above. The renderer resolves the call_id to its `function_call_output` item.
- **A refused steer** stays buffered and runs as the next turn's message (`runner/app.py:5408-5411,5810`). It gets its own turn position, and the renderer emits it there.

### 3.2 The renderer and the pi adapter

**Renderer:** `window(conversation, epoch) → list[TypedMessage]`. It's engine code, harness-neutral, and a pure function of the record.
1. The `epoch_block` item (by `epoch_block_item_id`), as the first user message.
2. Every item since the view start, in delivery order:
   - **user-role item that opens a turn:** its `context_prefix` blocks, then its words, exactly as sent;
   - **steer:** after the whole tool batch containing the result named by `delivered_after_call_id` (parallel calls stay together); never between a tool call and its result;
   - **assistant text and tool calls/results:** as stored. Results keep the §3.5 caps and pointers.
   - **Helper notices:** a user-role message in today's notice framing, capped (§3.5);
   - **interrupts:** the partial assistant text as stored. A tool call with no result gets a synthetic result "interrupted, effect unknown".
   - **skipped:** `reasoning` items and any thinking. Signatures don't survive, and the model accepts history without them.
   - **images:** inline only within the last 2 Rounds; older ones become `[image: <name>, in the Library]`. This is the one inexactness, and it's stated.

**pi adapter**
- **Writes** a pi session file: a version-3 header with `cwd` = the actual spawn cwd (pi takes its cwd from the header, `session-manager.js:1344`), unique ids in a linear `parentId` chain, paired toolCall/toolResult ids, and zero usage (pi estimates size, `agent-session.js:2118`).
- **Location:** `$HOST_SCRATCH/omnigent_pi/<conv>/<epoch>.jsonl`, under the launcher contract *ephemeral scratch, may vanish at any time*.
  - On Docker this is the container's `/tmp`. It survives runner and engine restarts and container stop/start, and is lost on container recreate (`eo/hosts/docker.py:316-322,348-351`; `host/bin/start.sh:15-17`).
- **Spawns** `pi --mode rpc --session <path>` instead of `--no-session`; compaction stays off. pi appends its own entries as it runs.

### 3.3 Respawn rule

The rule applies on every spawn of the Conversation's pi (runner restart, idle reap, model switch, crash). Throughout, `size(x)` counts in the same units as `assembled_estimate`: the ~24k fixed part plus rendered tokens, from the record, so it survives a runner restart (`eo/superchat/rollover.py:115-124`).

1. **Fast path: reuse the file.** Taken when all of these hold:
   - the file exists;
   - its tail is complete (no unfinished assistant message, no tool call without a result);
   - its last three user and assistant texts (after `person_words`) equal the record's.

   pi's own bytes are kept, so the prompt cache can hit within its lifetime (*measure*).
2. **Re-render: the file is missing or stale.** All three must hold:
   - the epoch's `epoch_block` item exists;
   - no turn-opening item in the epoch has `context_prefix = None`;
   - `size(window) ≤ 2 × assembled_estimate`. `assembled_estimate` comes from session labels (`superchat/rollover.py:120-122`); `size(window)` uses the fold's token counter over the rendered items (`rollover.py:803-813`).

   Render, write, spawn.
3. **Otherwise: start a new epoch.** Use today's assembled start (`handoff_reason`'s size rule, `superchat/rollover.py:127-159`), store a new `epoch_block` and spawn on it. This is exactly today's restart cost.

Cross-cutting rules:
- **Model switch:** keep the epoch and the file; pi supports cross-model history (`agent-session.js:2036-2040`).
- **The fast path and the re-render never move the view-start label and never schedule `index_retired_turns`.** Only branch 3 and the size handoff do, as today (ADR 0031:50).
- **Cost truth:**
  - a lost file never costs more than today (branch 3 is today);
  - with a warm cache the fast path costs less;
  - past the cache lifetime, a reused large file costs the same as a warm session that has been idle that long. This matches the owner's decision to keep warm sessions across idle (ADR 0031:54). It is gated as such (§7), not claimed as cheaper.

### 3.4 Line deltas, full block after any spawn
- Profile and Projects send changed lines only (file handles already do, `prompt_prefix.py:132-149`).
- The full block is sent on the first turn after any spawn: delta state is per process (`runner/app.py:6740`).
- Because `context_prefix` stores the deltas as sent, a re-render reproduces them.

### 3.5 Bulk at the door, exact reads back
- **Caps:** phase 0 lists every entry that can still exceed 16k chars (ADR 0031 already caps shell at 16k per field and session_history reads at 12k). Cap the rest, each with a pointer: `[clipped at N of M chars: session_history get call_id=<id> offset=N]`.
- **Helper notices:** 4k plus a pointer to the Helper transcript.
- **New action** `session_history get(call_id | item_id, offset, length)` returns stored output unclipped. It fixes the 300-char clip for exact reads.

### 3.6 Window swap: same rule, measured numbers
- Turn-start, size-only handoff as today.
- **T and the kept tail** come from the phase 0 sweep: T ∈ {40k, 80k, 120k} × tail ∈ {8k, 20k}. The 20k tail is the reference's and pi's default, but it isn't adopted without a quality gain worth its tokens.

### 3.7 Claude
- **Unchanged:** `_session_histories` (`runner/app.py:2750,4081`), `_load_history_as_input` and the flatten path stay, because Claude uses them.
- **Follow-up after phase 3's gate:** test whether the Claude Agent SDK's resumable session format can be written from the same render (*verify then*). If it can, Claude gets an adapter and the flatten path is deleted. If not, the two paths stay, and docs/context.md says so.

### 3.8 Wake and hold
- **Intent wake:** app open or focus, owner-only, a single-flight launch lock shared with the message path, never eval persons, never the managed relaunch branch.
- **Hold:** the Conversation's pi stays up 12 h after a phase 0 RSS check. Helpers stay at 1 h.
- **No rhythm prediction, no cache heartbeats** (rejected earlier: personal data and cost).

### 3.9 File hygiene
- **Start sweep:** on runner start, delete scratch dirs of Conversations that aren't live.
- **Reset or delete:** deletes the dir when the runner is alive, else the next start sweep does.
- **Size:** the file is bounded by the epoch (T) and the image rule. No separate size cap.
- **Readability:** the Assistant's shell can read the file. It holds what `session_history` already returns plus the system prompt, which is in this public repo. Accepted, and documented in docs/context.md.

---

## 4. Long-term memory

### 4.1 Profile: the always-on slice
- **Content:** claims at confidence ≥ 0.75 (as today), sent as line deltas (§3.4).
- **New hard cap:** above 1.5k tokens, the lowest-salience lines leave the profile and stay searchable. The reference's memory file has no cap and grew 74% in one run; ours can't.

### 4.2 In-turn writes: explicit only
- **AGENTS.md change:** during the turn, save only an explicit standing instruction or correction ("remember", "always", "never", "from now on", "that's wrong, it's …"). Remove "`memory_search` first".
- **Inferred facts** go to the existing upkeep, which already runs after each fold.
- **Why:** it removes up to two model round trips before the answer on memory turns.

### 4.3 Background: one clock fewer, nothing merged
- **Unchanged**, each with its own prompt, verification, lease and retries: the fold (per Conversation), upkeep (extract → verify → classify → apply), the quiet-moment passes and their 30 s ticker.
- **Phase 0 measures** the share of upkeep runs that only the hourly sweep produced (`server/memory_upkeep.py:184`).
- **If it is under 5%** and those runs changed nothing, the sweep is deleted in phase 0's follow-up commit. It isn't a phase of its own.

### 4.4 One host indexer, one cursor
- **Replaces three writers:** the STATE.md/journal mirror (`runner/memory/sync.py:304,362`), `index_retired_turns` (`:449`) and `backfill_turns` (`:550`).
- **New indexer:** cursor = last indexed item id. It indexes every settled turn and the checkpoint journal lines straight from the record.
- **Recall keeps the current-epoch filter**, so it never returns what is already in the window.
- **Before the mirror goes:** amend ADR 0017 and confirm every STATE.md reader (the indexer, files pane, Settings).
- **Kept:** the txtai claims index. Engine-only consumers (Lookout Judge, upkeep dedupe) need it.

---

## 5. What enters the window from outside
1. **Proactive deliveries:** only the delivered text. Research stays in the Helper transcript.
2. **Helper results:** a capped notice plus a pointer (§3.5).
3. **Checkpoint sections** (fold prompt): keep "pending, prepared not delivered" and "open commitments". Delivered items leave the summary.

---

## 6. Measurement (phase 0)

| Measure | How | Decides |
|---|---|---|
| **Latency matrix**: warm, cache-cold, pi reaped, runner exited, engine restart × short/long | existing profiler (eval person through the engine API) | gates for phases 3 and 6 |
| **First-call input tokens and cache_read** after a restart: today's start vs fast path vs re-render | llm-context records | §3.3 cost truth |
| **Window makeup** by kind; entries > 16k chars | llm-context records | §3.5 caps |
| **Quality:** handoff_probe + offsite.json, the hint-free memory test, LongMemEval short via the evaluation platform | existing scripts | every gate |
| **T × tail sweep** | handoff_probe at T ∈ {40k, 80k, 120k} × tail ∈ {8k, 20k} | §3.6 |
| **Upkeep runs by trigger** | `memory_upkeep_runs` | §4.3 |
| **RSS** of idle pi, and pi + 3 Helpers | host ps | §3.8 |

---

## 7. Phases

Each phase has its own flag and stays off until its gate passes on the train stack.

| # | Phase | Flag | Gate | Rollback |
|---|---|---|---|---|
| 0 | Measurement; then delete the hourly sweep if redundant | — | baselines written | — |
| 1a | §4.2 explicit-only saves | `memory.in_turn_explicit_only` | answer-ready time on memory turns ↓; save rate held; false saves not ↑ | flag off |
| 1b | §3.4 line deltas | `prefix.line_deltas` | profile tokens per turn ↓; probe held; full block after every spawn | flag off |
| 1c | §4.1 profile cap with demotion | `memory.profile_cap` | memory eval held (no needed line dropped); probe held | flag off |
| 2 | §3.5 caps + `session_history get` | `context.entry_caps` | window growth ↓; probe held | flag off |
| 3 | §3.1–3.3 + §3.9 record fields, `epoch_block`, renderer, pi adapter, respawn rule, hygiene | `pi.record_rendered_window` | restart first word ≈ warm; with a warm cache, first-call input ≤ today's; past the cache lifetime, equal to a warm session's; probe held on fast path **and** re-render; conformance + RPC smoke green | flag off → `--no-session` |
| 4 | §4.4 one indexer | `knowledge.single_indexer` | recall precision held; journal-source parity | flag off → old writers |
| 5 | §3.8 wake + hold; §3.6 tail if the sweep says so | `wake.intent`, `host.keep_warm_s` | cold first word ↓; RSS headroom | flag off |
| — | §3.7 Claude adapter | later | after phase 3 | — |

---

## 8. Tests (offline, deterministic)
- **Renderer golden tests** (record fixtures → typed window):
  - steers taken (by `delivered_after_call_id`, incl. `"turn_start"`, and after a parallel tool batch) and refused (next turn);
  - notice and wake turns carrying their own `context_prefix`;
  - Helper notices;
  - an interrupt with a dangling tool call;
  - images inside and outside the last 2 Rounds;
  - attachments, `reasoning` items skipped, a model switch.
- **Record fields:**
  - `context_prefix` and `delivered_after_call_id` written idempotently through the runner-only endpoint (other callers rejected);
  - neither field appears in the items API, SSE, `check_task`, search, transcript or extraction;
  - `None` vs `[]` handled;
  - `epoch_block` accepted only from the runner credential; excluded from the items API, SSE, search and replay; included in export and delete;
  - the label references the block by id;
  - the enum-codec parity test.
- **pi conformance:** render → session file → the pinned pi's `SessionManager.open` in node. Assert entries, ordering, tool pairing and header cwd.
- **pi RPC smoke (no model):** spawn the pinned pi with `--session <rendered>` in RPC mode, send `get_messages`, and assert it equals the rendered window. Runs in CI against the pinned version.
- **Respawn rule:** fast path; tail mismatch; unfinished tail; missing file; missing `context_prefix`; large window → new epoch; model switch; view start and indexing untouched on branches 1–2.
- **Hygiene:** start sweep, live vs dead Conversations.
- **Indexer:** cursor resume, duplicates, current-epoch filter.
- **Explicit-save rule:** a memory-eval case set (explicit, implicit, correction, card pick).

---

## 9. Migration
- **`context_prefix`, `delivered_after_call_id` and `epoch_block`** are written from the flag's first turn. Older epochs lack them, so their first respawn takes branch 3 (today's behavior). Nothing is backfilled.
- **Indexer:** its cursor starts at 0 and backfills in bounded steps (today's limits). Mirror files are deleted only after the parity gate.
- **Memory:** nothing is migrated.

---

## 10. What the person gets

| Promise | Mechanism | Proof |
|---|---|---|
| After a restart, the Assistant still has the exact conversation, not a summary. After a deploy, the same holds when the window is small enough to re-render (≤ 2× the assembled start); otherwise it starts from the checkpoint as today | §3.1–3.3 | probe on fast path and re-render |
| A restart no longer feels cold | fast path + intent wake + hold | latency matrix |
| Answers on "remember this" turns start as fast as any other | §4.2 | answer-ready time |
| "What exactly did that file or tool say?" gets the exact text | §3.5 | pointer-follow rate |
| Memory stays small and current | §4.1 cap + existing supersession | profile tokens |
| A lost file never costs more than today; a reused file costs what a warm session costs | §3.3, cost gates | first-call input tokens per start kind |
| Fewer moving parts | 3 index writers → 1; hourly sweep removed if redundant; pi stops flattening history | Appendix A diff |

## 11. Decisions needed
1. Amend ADR 0031 (option 69): pi spawns on a disposable session file rendered from the record.
2. Record fields `context_prefix` and `delivered_after_call_id`; new hidden item type `epoch_block`; one runner-only item-data endpoint.
3. Amend ADR 0017: drop the STATE.md/journal mirror for one indexer.
4. Explicit-only in-turn saves.
5. Profile cap with demotion.
6. Accept that the Assistant's shell can read its own window file.

## Appendix A: inventory (code, 2026-11-11)

**Background processes (8)**
1. Fold: `server/memory_upkeep.py:269-346`.
2. Upkeep + hourly sweep: `:101`, `:184`; `memory/upkeep.py:952`.
3. Quiet-moment passes (idle, finalize, dreaming, people, topic seed) as notes Helpers: `superchat/daily_notes/quiet_moment.py:365,415,510,560,604-633`.
4. STATE.md/journal mirror: `runner/memory/sync.py:304,362`.
5. Turn indexers (two): `sync.py:449,550`.
6. Knowledge indexer: `superchat/knowledge/indexer.py:149`.
7. Morning brief reader.
8. Lookout Judge reader.

**`memory_claims` writers (5):** upkeep; idle pass; dreaming; People; `memory_remember`. The UI also edits.

**Search paths (3):** `session_history` search; the host `turns` index; txtai claims.

**Module sizes (lines):**

| Module | Lines |
|---|---|
| superchat/rollover.py | 916 |
| context/rollover.py | 1,358 |
| memory/ | 2,305 |
| daily_notes/ | 1,518 |
| runner/memory/ | 1,100 |
| knowledge/ (about half file RAG) | 5,820 |
| prompt_prefix.py | 546 |
