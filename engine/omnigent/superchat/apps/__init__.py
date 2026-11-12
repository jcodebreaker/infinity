"""Apps: a saved HTML file published at its own web address, with an audience and view stats.

Layout: ``routes`` (``/v1/artifacts/{id}/publish``, ``/v1/published``), ``tools`` (the
``artifact_publish`` tool def), ``handlers`` (runner side), ``feature`` (registration). The
publication rows stay in the artifacts store; this capability sits on top of artifacts.
"""

from __future__ import annotations

from omnigent.superchat.apps.feature import APPS_FEATURE

FEATURE = APPS_FEATURE

__all__ = ["APPS_FEATURE", "FEATURE"]
