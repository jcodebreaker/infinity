"""Document text that an older gateway stored inside a person's message.

A message that attaches files is stored as the person's words plus one reference line per file.
Older gateways also stored the files' text, in an ``<attachment_context>`` block after the lines.
That text is document data, not the person speaking, so every reader of a user's message (the
transcript, search, the Memory Profile's evidence, Helper activity titles) cuts it with this one
function. Messages stored today never carry the block: the host builds it at turn start.
"""

from __future__ import annotations

import re

_BLOCK = re.compile(r"<attachment_context>.*?</attachment_context>\n?", re.DOTALL)


def strip_legacy_attachment_context(text: str) -> str:
    """*text* without a stored ``<attachment_context>`` block (and the blank lines it leaves)."""
    if "<attachment_context>" not in text:
        return text
    return re.sub(r"\n{3,}", "\n\n", _BLOCK.sub("", text)).strip()
