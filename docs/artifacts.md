# Artifacts

Files the assistant makes for the person, how they are versioned, edited, published and kept up
to date, as of 2026-10-19. Engine code is under `engine/omnigent/superchat/` (`artifacts/`,
`apps/`, `live/`, `sheets/`); the web side is `web/app/src/features/artifacts/`. How a message links
to an artifact (the chip) is in [conversation.md](conversation.md).

## What an artifact is

A deliverable file the assistant saved from its host with `artifact_save`. The engine stores the
bytes (on the `/data` volume, or S3/R2 when `OMNIGENT_ARTIFACT_URI` is set) and one row per version
(`artifacts/store.py`). Allowed kinds are `html`, `md`, `pdf`, `docx`, `xlsx`, `csv`, `pptx`,
`png`, `jpg`, `txt` and `json`, up to 25 MB (`artifact_kinds.py`). An HTML file named `*.deck.html`
is a deck ([decks.md](decks.md)).

The model's tools are `artifact_save`, `artifact_list`, `artifact_delete` and `artifact_publish`.
A successful save shows in the transcript as a `file` block (the artifact card), and the files a
helper saved are named in its result. The person's Library lists every artifact. Routes:
`POST/GET /v1/artifacts`, `GET /v1/artifacts/{id}`, `GET /v1/artifacts/{id}/content`,
`DELETE /v1/artifacts/{id}`. HTML previews are served under a strict content security policy.

## Versions and origins

Versions of one file share `(owner, conversation, name)`: saving the same name again in the same
conversation adds a version instead of a new file. A save whose bytes equal the newest version's
adds nothing: the route answers that version with `unchanged: true`, and `artifact_save` and
`artifact_patch` return the error `artifact_unchanged` saying nothing was saved
(`artifacts/routes.py`, `artifacts/handlers.py`). A save or patch of a deck also returns `layout`,
the deck check of the saved bytes ([decks.md](decks.md#authoring)). Every version records its origin:

| Origin | Made by |
|---|---|
| `ai` | the assistant or a helper, with `artifact_save`; a file's studio also with `artifact_patch` |
| `manual` | the person, by hand in an editor (`PATCH /v1/artifacts/{id}/edit`, or a sheet's `PATCH /v1/artifacts/{id}/table`) |
| `restore` | the person, restoring an earlier version (`POST /v1/artifacts/{id}/restore`) |
| `update` | a live page or sheet update ([Live pages](#live-pages)) |

A version saved during a turn records the message that started the turn, which is what the
editor's version menu links back to ([conversation.md](conversation.md#a-version-links-to-a-message)).

## The canvas editor and the element ask

Decks, HTML pages and mini apps open in the canvas editor (`features/artifacts/editor/`). Its
controls turn into deterministic source patches (`artifacts/patches.py`): `set-text`, `set-style`,
`set-attributes`, `remove-element`, `duplicate-element`, `set-theme` (decks only) and
`set-full-source` (undo, redo, and the document editor for Markdown and text). Each patch finds its
element by `data-superchat-id` and rewrites only that element's span, so every other byte is
untouched. A patch that cannot be applied exactly (a duplicate id, mixed text and tags) is refused
with a 409 that tells the person to ask instead of guessing. A stale `base_version` is an
`artifact_stale` 409, and the editor reloads.

The element ask is the editor's "Ask" box (`features/artifacts/ask/`,
`editor/html-ask.ts`): the person selects up to 12 elements, and the message travels with a
`<superchat-element-request>` block ahead of their words naming each element's id, slide, text and
computed style, every field sanitized as untrusted data. The transcript shows a chip instead of the
block. Only the selected elements change. The patch engine and the ask are ported from Open Design
(see `NOTICE`).

## The studio's editor

A file's studio ([ADR 0024](adr/0024-the-artifact-studio.md)) is a child session of helper type
`editor`. Each of its turns starts with the file line (name, group id, newest version, who saved
it, workspace path) and, for a deck, a slide outline derived by code (`decks/outline.py`): one
line per slide, `Slide 3: <element id> "<text>" · …`, at most 40 slides and 2,000 characters, the
footer left out.

The editor prefers `artifact_patch` (`POST /v1/artifacts/{id}/patch`) to a rewrite: the same
patches as the canvas, addressed by `data-superchat-id`, applied to the newest version when
`base_version` still names it, saved as an `ai` version linked to the studio turn's message, then
written to the workspace file. Only the file's own studio may call it; a stale base is an
`artifact_stale` 409, and a patch that cannot be applied exactly tells the editor to edit the file
and save it with `artifact_save` instead.

Two writers, one rule each way. A version another session saves while the studio's turn runs (or
within five minutes of its last turn) is told to the studio as `Super Chat saved v9 while you were
editing; v10 is based on v8`, steered into the running turn or stored for the next one, and the
studio's next save is stale. The Conversation's artifact hint marks a file whose studio has a turn
running ` — being edited in its studio`, and a live update waits until that turn ends.

At most three studio turns run at once on a host; a fourth waits, its session status showing
`blocked_on: "Finishing another file first"`. A studio's harness stays warm for ten minutes after
its last turn or its last open signal (`POST /v1/artifacts/{id}/chat/open`, sent when the studio
opens and while it stays open), then is released; the next request cold-starts it. The open
signal reaches only a runner that is already up and never wakes the host.

In the activity feed each studio turn is one activity (`kind: "turn"`, `source: "studio"`) with
`artifact: {group_id, name, round_id?}`, `round_id` being the Round that created the file (its
first version's); the panel hangs the file and its edits under that request's row. The studio
session itself is never a row or a Helper. The studio chat header counts as edits the versions
saved after the first, not the requests sent.

## Write-back

A hand edit must reach the file in the host before the assistant touches it again. At the start of
the next turn the runner (`artifacts/writeback.py`) takes every `manual`, `restore` or `update`
version not yet delivered (`GET /v1/artifacts/pending-edits`), writes its bytes to the workspace
path the artifact was saved from, acknowledges it (`POST /v1/artifacts/{id}/delivered`), adds one
line per file to the turn prefix, and journals it:

```text
The person edited launch-budget.xlsx by hand (v2 → v3): Changed the Q4 venue line. Treat v3 as current.
```

A failed write is not acknowledged, so the next turn retries, and the note tells the assistant not
to overwrite the person's edits.

The pending list is scoped by the session id the runner passes, and artifacts are keyed to the
Conversation, so a Helper's turn finds nothing. A file's studio session
([ADR 0024](adr/0024-the-artifact-studio.md)) resolves the Conversation the way saves do and writes
back only its own file, at the start of each of its turns.

## Publishing

An HTML artifact can be published as a web page (`apps/`): `POST /v1/artifacts/{id}/publish` with
an audience of `owner`, `org` or `link`, optionally pinning a version (default the newest).
Republishing keeps the address. The engine serves the page at `GET /apps/<slug>`, checks the viewer
against the audience, and locks the page into an opaque origin with its response headers. View
counts are kept per publication (a repeat open by the same viewer within 30 seconds is not counted
again). `DELETE /v1/artifacts/{id}/publish` takes it offline. The assistant publishes with
`artifact_publish`.

## Live pages

A live page is an HTML artifact that keeps itself up to date from source files in the host; a live
sheet (CSV, XLSX) follows the same rules for cells (`live/`, `sheets/`). The assistant turns one on
with `artifact_keep_updated` (`artifact_stop_updating` and `artifact_live_settings` change or
stop it), and the person can from the page's popover. Each live page is one scheduled task
(`kind="live_artifact"`) bound to the conversation, run as a `worker` helper.

- **Triggers.** A source file changing in the host: the host polls the modified time of live
  pages' sources every 30 seconds (`live/watcher.py`) and reports changes; the engine debounces
  per page for 2 minutes. Also the schedule as a safety net, and "Update now".
- **What an update may change.** It works on the newest version and changes facts by
  `data-superchat-id` only (a sheet: by cell, never a formula cell). A part the person changed is
  theirs: the update becomes a suggestion ("source says X, you wrote Y"). A part the person deleted
  stays deleted, and a value they undid is declined until the source changes. The person's own
  "What to update" note is read by every run. The rules are one pure function, `live/logic.py`.
- **Outcome.** A change is one new version, origin `update`. No change moves only `checked_at`.
  The run waits while the person edited by hand in the last 10 minutes. Changes that cannot be
  placed twice in a row pause the schedule; three failed runs in a row pause it and post one note
  in the conversation from the engine. A live sheet's run sees the first 400 rows of each sheet.
- **Capacity.** The host reports its free memory and load per CPU with the watcher's call; the
  tighter gives a cap of 1 to 6 concurrent runs (`room_for`), 3 without a report in the last 2
  minutes. A due run is queued, never dropped.
- **Published live pages** follow updates by default. An update with suggestions, unplaced
  changes, or sitting on unpublished hand edits is held back, and the page says why.

Routes: `GET /v1/me/live-pages`, and `/v1/artifacts/{id}/live` with `run`, `undo`, `accept`,
`reset`, `publish`, `context` and `save` actions.

Known limit: the scheduler's timers are per engine process, so a second engine replica would fire
every run twice.
