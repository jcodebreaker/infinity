# A file opens in one studio: its conversation on the left, the file on the right

> **Status:** Accepted 2026-10-19. Supersedes the parts of [ADR 0024](0024-the-artifact-studio.md) named under "Superseded"; its engine shape (the `editor` type, one studio session per file, `artifact_patch`, `base_version`, concurrency) stays. A clickable mock-up was approved by the owner before the build.

Today a file takes two steps and two surfaces. The card's *Open* puts a side panel beside Super Chat (`features/artifacts/ArtifactPanel.tsx`) with its own "Ask superchat to change this slide…" box; typing there or *Edit* opens a second, full-window surface (`studio/ArtifactStudio.tsx`, a `Dialog` over the panel) with "This file's chat" on the **right**. That chat starts empty: it shows `v1 · From Super Chat` but not the request that made the deck or the hand-back sentence, so the person who just talked about the deck finds its story missing. A new version is felt as a reload: the panel's preview is a `srcDoc` iframe (`DeckPreview` → `SandboxedHtmlViewer`), so new bytes restart it on slide 1; the panel is keyed by version id (`useChatArtifacts`: `key={open.id}`); picking a version sets `status: "loading"` and unmounts the editor (`useArtifactContent.selectVersion`); panel and studio each poll `listVersions` every 4 s and refetch on `artifacts.changed {root_id, group_id}`, which carries no version, so each save costs two round trips per surface. Only the studio's `EditStage` already swaps a hidden second frame in place. In the live app the studio thread first renders version markers alone and the request and reply pop in a moment later.

The person said it plainly: open a file, see the file with its conversation beside it, holding all of it; select something and it shows in that conversation; new versions arrive without the page reloading.

## Decision

**One view.** *Open* on a file card, a Library item or a citation opens the **File Studio**: the file's conversation on the left with its composer at the bottom, the file on the right (title bar: name · kind · version N, Present, Download, ⋯, close; the slide rail; the canvas). There is no side panel and no second step. *Close* (or Escape with nothing selected, or the browser's back) returns to Super Chat at the card. The studio is a route state (`?file=<groupId>`), not a dialog over a panel, so back works and a reload keeps it. Hand editing (inspector, palette, undo) is a mode of the same canvas, entered by selecting an element; there is no *Edit* button and no *Done*.

**The left column is the file's studio session, with a read-only prelude.** The session stays the ADR 0024 one (isolated, never in the Conversation's context). It opens with a **prelude** derived at read time, not copied: for each version whose origin is Super Chat, the Round that saved it (`artifacts.round_id`, ADR 0029) gives the person's ask (the Round fold's `ask`) and the Assistant's last words in that Round (the newest message item for that `round_id`, `rounds/store.item_rounds`). They render as the person's bubble and the reply under an "In Super Chat" label with *Show in Super Chat*, placed before that version's divider. `POST /v1/artifacts/{id}/chat` returns `prelude: [{version, round_id, ask, handback, item_id, at}]` with the session id, in the same response, so the thread paints once. Reasons: one place holds the whole story; copying messages would duplicate them and let them drift; the Round is already the recorded unit. The editor's per-turn prefix gains one line, "Made in Super Chat from: <ask>", so it knows the brief.

**Selection becomes a chip in the left composer.** Clicking an element on the canvas outlines it and puts `Slide 1 · cover-title` in the composer; clicking a slide in the rail with nothing selected scopes to `Slide 2`. Sending carries the existing `<superchat-element-request>` block (`editor/html-ask.ts`, `data-superchat-id`), unchanged; the sent bubble shows the chip as today (`thread.ts` `splitElementAsk`). The inspector opens beside the canvas for hand edits on the same selection.

**New versions swap in place.** `artifacts.changed` gains `version_id`, `version`, `origin` and `changed_ids` (the element ids an `artifact_patch` touched, or empty for a rewrite). The studio calls `adoptVersion(version_id)` (silent: no loading state) and `EditStage` loads it in its hidden frame, `place()`s it on the slide on screen with the same selection and scroll, then swaps; the bridge gets `superchat:edit-flash {ids}` and rings the changed elements for 1.2 s. The version label ticks; a divider `v3 · From this chat` lands in the thread. There is one `useArtifactContent` per open file; its 4 s poll runs only while the family stream is down. A version picked from the menu also swaps silently. Nothing is keyed by version id.

**While a turn runs** the left column shows the turn's live strip (ADR 0029, the studio turn's own Round); the canvas stays readable and navigable, and only hand edits wait (ADR 0024's inert rule narrowed: selection is allowed so the next request can be scoped).

**Phone.** The file on top full width, the conversation as a bottom sheet with a peek row (latest line + composer); selecting an element raises the sheet with the chip in it.

**Kinds without a canvas** (PDF, DOCX, PPTX, image) open in the same studio with a viewer on the right; their conversation and composer work the same way.

## Superseded in ADR 0024

- "**Opening is not editing.** The file card opens the file beside Super Chat" and "**Editing opens the studio.** *Edit*, or starting to type in the panel's … box". Opening is the studio.
- "‹ Super Chat closes it and returns … with the panel still open on the file". It returns to the conversation.
- "The studio subscribes … reloads the canvas on a save". It swaps in place.
- "While the assistant works, the canvas is read-only … no selection can start". Selection may start; hand edits wait.
- Web: "opened from the panel's Edit and composer"; "The main composer keeps `context.openArtifact` when the panel is open" (there is no panel; the studio is a route).

## Deleted

`ArtifactPanel.tsx` and `panel-layout.ts` (split/overlay widths), the panel's `AskBox` with `onHandOff`/`AskDraft`/`sendInitialDraft`, `ArtifactEditor`'s `onEdit`/`onAskSend` panel hooks, the `Dialog` wrapper in `StudioView`, `useArtifactEditing`'s Edit/Done toggle, the second `useArtifactContent` instance, `DeckPreview`'s `srcDoc` path inside the studio, and the `key={open.id}` remount in `useChatArtifacts`.

## Migration

Nothing stored changes. Existing studio sessions keep their transcript; the prelude is computed, so old files get it for free when their versions have a `round_id`. Versions saved before ADR 0029 have none: their prelude falls back to the chat item that saved them (`all_versions[].item_id`) and the message before it, or shows nothing.

## Risks

- A Round whose last words are long (the Paracas hand-back is 20 lines): the prelude shows the first paragraph with *More*, and *Show in Super Chat* for the rest.
- Two tabs, or Super Chat saving while the studio is open: unchanged ADR 0024 rules; the swap follows whichever version is newest unless an older one is picked.
- `changed_ids` is empty for rewrites: no ring, still no reload.
- Losing the side-by-side "deck next to Super Chat" view: the left column *is* the conversation for this file; Super Chat is one click away.

## Build plan

1. Engine: `prelude` in the chat route (version → round → ask + hand-back); `artifacts.changed` gains `version_id`, `version`, `origin`, `changed_ids`. Checked live: the chat route's response carries the prelude; the stream event carries the version.
2. Web: the studio as a route state; card, Library and citations open it; delete the panel and the hand-off. Review: no dialog, back works.
3. Web: the left column (prelude rows, version dividers, live strip, composer with chip); thread paints from one response.
4. Web: in-place swap (adopt silent, place, flash), one content hook, poll only when the stream is down. Browser check: send on slide 2, stay on slide 2, no flash.
5. Phone sheet and non-canvas kinds. Docker stack and browser matrix before merge.
