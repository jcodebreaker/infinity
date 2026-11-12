"""Organization admin: the admin-only ``/v1/admin/{users,usage}`` routes.

Layout: ``routes`` (the router: people, usage, suspend/resume/remove). Sits on top
of ``models`` and reads its stores through the package entry point only; the budget and the
organization's connections stay in ``models``.
"""

from __future__ import annotations

from omnigent.superchat.admin.feature import FEATURE

__all__ = ["FEATURE"]
