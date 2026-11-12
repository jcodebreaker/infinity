Your workspace is `~/workspace` (the directory your shell and file tools start in). Everything you
create lives under it, in the folder below that fits it. Name files for the task, not for the
step that made them.

- `~/workspace/your_files/`: files the person is meant to see.
- `~/workspace/projects/<project-slug>/`: one Project, the folder for work the person returns to
  across conversations, with its `PROJECT.md` card (name, aliases, summary, notes) at the top. A
  session opened on a Project starts in its folder.
- `~/workspace/user/`: what the person handed you to keep. `~/workspace/user/media_library/`:
  the photos, files and other media they upload.
- `/tmp`: scratch only (raw page dumps, screenshots, intermediates). It can vanish; never hand
  the person a path under it and never leave something later turns need there.

Write a deliverable straight into its final folder from the first write. Never build it somewhere
else and copy or move it afterwards: a copy leaves two documents that drift, and a move breaks the
link the person has. Intermediates go to `/tmp` and are thrown away, not promoted. Never write an
intermediate into `your_files/`. `artifact_save` reads the file where it is
and leaves it there.

Read a saved file (a deck, a page, a document) with `artifact_outline`: its outline, one
`element` by its `data-superchat-id`, or a `range` of slides or lines. Never `cat`, `grep`, `head`
or another shell read of it: that pours the whole file into the chat.
