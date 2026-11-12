"""Fail when a branch adds a line that brings back a retired word (ADR 0029).

Reads only the lines the branch adds (``git diff BASE...HEAD -U0``) under the web's message
catalogs, ``docs/`` (not the glossary, which names the retired words, nor the ADRs, which keep
their history) and the agent templates, and prints each offender as ``file:line: text``.
Run from the repository root:

    python3 engine/scripts/retired_words.py [BASE]   # BASE defaults to origin/main
"""

from __future__ import annotations

import re
import subprocess
import sys

#: Case-sensitive, whole words. "sub-agent" is retired too, but it still has over a thousand
#: upstream hits; the naming PR (PR 15 of the ADR 0029 train) adds it here once they are gone.
RETIRED = re.compile(r"\b(?:Muse|Super Chat|Nova)\b")
PATHS = (
    "web/app/src/locales",
    "docs",
    "engine/agents/templates",
    ":(exclude)docs/glossary.md",
    ":(exclude)docs/adr",
)
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


def offenders(diff: str) -> list[str]:
    """``file:line: text`` for each added line of a ``-U0`` diff that carries a retired word."""
    found: list[str] = []
    path, line = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else ""
        elif hunk := _HUNK.match(raw):
            line = int(hunk.group(1))
        elif raw.startswith("+"):
            if path and RETIRED.search(raw):
                found.append(f"{path}:{line}: {raw[1:].strip()}")
            line += 1
    return found


def main(argv: list[str]) -> int:
    base = argv[0] if argv else "origin/main"
    diff = subprocess.run(
        ["git", "diff", f"{base}...HEAD", "-U0", "--no-color", "--", *PATHS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = offenders(diff)
    for entry in found:
        print(entry)
    if found:
        print(
            f"retired words: {len(found)} added line(s) say Muse, Super Chat or Nova; "
            "use Assistant or Conversation (docs/glossary.md)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
