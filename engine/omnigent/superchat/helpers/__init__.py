"""Helpers: the one-call ``start_helper`` tool a Muse (or coordinating Helper) hands work off with.

Layout: ``tools`` (schema), ``handlers`` (runner side, over the generic sub-agent create path),
``saved_files`` (files a Helper saved, carried in its result), ``feature`` (registration).
"""

from __future__ import annotations

from omnigent.superchat.helpers.feature import HELPERS_FEATURE

FEATURE = HELPERS_FEATURE

__all__ = ["FEATURE", "HELPERS_FEATURE"]
