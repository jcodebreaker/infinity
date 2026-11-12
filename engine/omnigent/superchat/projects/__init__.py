"""Projects: folders under ``~/workspace/projects/<slug>/`` the Muse opens on its own.

A Project is a folder with a ``PROJECT.md`` card (see ``card``). Each turn the runner lists the
cards (``block``); the Muse picks one and calls ``open_project`` (``handlers``), which moves the
session's working directory through the generic ``PUT /v1/sessions/{id}/workspace`` route.
The person may choose a chat's folder themselves (``choice``); their choice wins over
``open_project``. Layout: ``card`` (read cards), ``block`` (the per-turn list), ``tools`` (tool
def), ``handlers`` (runner side), ``choice`` (the person's mark), ``feature`` (registration).
Nothing scans or indexes in the background.
"""

from __future__ import annotations

from omnigent.superchat.feature import register_project_resolver
from omnigent.superchat.projects.feature import PROJECTS_FEATURE
from omnigent.superchat.projects.handlers import resolve_project

register_project_resolver(resolve_project)

FEATURE = PROJECTS_FEATURE

__all__ = ["FEATURE", "PROJECTS_FEATURE"]
