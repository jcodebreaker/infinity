"""The engine's entrypoint is ``deployment/entrypoint.py``, outside the package.

These tests import it as ``deployment.entrypoint``, so the repository root goes on ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
