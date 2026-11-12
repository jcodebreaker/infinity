"""Live artifacts: a page that keeps itself up to date (rules: ``docs/artifacts.md``).

Built on ``superchat/artifacts``, which knows nothing of this folder. ``watcher`` is the host
side: it tells the engine when a source file of a live page changed.
"""

from __future__ import annotations

from omnigent.superchat.live.feature import LIVE_FEATURE

FEATURE = LIVE_FEATURE
