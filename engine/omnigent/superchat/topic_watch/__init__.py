"""The Library-file source of the Lookout (ADR 0038; what remains of 0035's topic watch).

Topic watch's hourly task, cursors, seen-set and Light judge are gone; its matching rules moved
into the Lookout's code check. What is left is the push: when the engine writes an artifact
version with origin ``update`` (a live page refreshed from its sources) it becomes one signal
(``library.on_version_saved``). The person's and the assistant's own saves are not news.

No ``FEATURE`` and no imports of other capabilities: the composition root registers the save
hook and hands in the Lookout's entry point.
"""
