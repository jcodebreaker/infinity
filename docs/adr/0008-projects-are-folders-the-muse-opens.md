# Projects are folders the Muse opens on its own

People return to the same work across conversations ("the deck", "Dana's report") and expect the Muse to know which work they mean and act inside it, the way a coworker would. A comparable agent keeps no file metadata, never changes its working directory, and finds files by memory, the conversation and keyword search, which it admits degrades for older files. We add the smallest thing that closes that gap: a Project is a folder under `~/workspace/projects/<slug>/` with a `PROJECT.md` card (front matter: name, aliases, one-line summary, people, linked Goal, updated). The Muse writes and updates the card during the conversation, as it does memory, when work clearly spans more than one conversation, when the person asks, or when a Goal is created. Each turn the runner lists `projects/` and adds the cards' name, aliases and summary to the turn; the Muse picks the Project from that list and the conversation, and calls `open_project`, which sets the session's working directory through a generic Omnigent route so its tools and any Helper it starts act inside the folder. When two Projects fit, it asks. Projects and Goals are separate: a Goal is an outcome with a plan and may link a Project.

## Considered options

- **Index every file (metadata, hashes, embeddings).** Rejected: continuous scanning and hashing for a workspace of hundreds of files; coding agents that tried vector indexes went back to on-demand `find` and `grep`, which this keeps for finding files inside a Project.
- **A background job that infers cards from folder contents.** Rejected: extra model calls and scanning for something the Muse already knows when it starts the work.
- **One side chat per Project, picked by the person.** Rejected as the main path: the person has to choose first, which is the step this removes. A side chat can still be pinned to a Project.
- **Projects are Goals.** Rejected: much recurring work (research, a deck) has no plan or due date.

## Consequences

- Omnigent gains a generic route to change a session's working directory, with the runner's cached value invalidated; Helpers start in their parent's working directory.
- The per-turn Project list costs one directory listing and a few hundred tokens; nothing runs in the background.
- Finding a file inside a Project stays on-demand (`find`, `grep`, modification time).
