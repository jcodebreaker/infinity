"""Single source of truth for Omnigent session presentation labels.

The values are tiny string constants that must match across call sites;
centralizing them here keeps one symbol per concept instead of
stringly-typed literals.
"""

from __future__ import annotations

# Label key stamped on wrapper-owned conversations. Reserved for the
# ``omnigent.*`` namespace; never reused for guardrails / policy labels.
WRAPPER_LABEL_KEY = "omnigent.wrapper"

# Label key + value that make the Web UI offer the session's terminal view
# (the Web UI gates on ``labels["omnigent.ui"] == "terminal"``). Stamped when
# the runner hosts the session's REPL terminal. Centralized here so the fork
# route can re-derive it for a switched agent rather than copying the
# source's.
UI_MODE_LABEL_KEY = "omnigent.ui"
UI_MODE_TERMINAL_VALUE = "terminal"
