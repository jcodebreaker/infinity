"""The host's ``memory/`` folder: a readable copy of the journal and the working list.

See :mod:`omnigent.runner.memory.files` for the file layout and :mod:`omnigent.runner.memory.sync`
for what feeds it. The folder is a projection: the compaction items stored with the chats are
the only source, and nothing in the engine reads these files back.
"""
