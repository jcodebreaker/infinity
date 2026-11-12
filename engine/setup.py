"""Build hook: writes ``omnigent/_build_info.py`` (the source commit) before ``build_py``.

Everything else about the package is declared in ``pyproject.toml``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


class _GenerateBuildInfo(build_py):
    """Write ``_build_info.py`` into the source tree, then run the stock ``build_py``."""

    def run(self) -> None:
        # Keep generated names and types aligned with omnigent/_build_info.pyi.
        target = Path(__file__).resolve().parent / "omnigent" / "_build_info.py"
        target.write_text(
            '"""Auto-generated at build time; do not edit."""\n\n'
            "from __future__ import annotations\n\n"
            f"BUILD_TIME_EPOCH: int = {int(time.time())}\n"
            f"COMMIT_SHA: str = {_git_sha()!r}\n",
            encoding="utf-8",
        )
        super().run()


setup(cmdclass={"build_py": _GenerateBuildInfo})
