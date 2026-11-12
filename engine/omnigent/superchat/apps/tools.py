"""The ``artifact_publish`` tool: publish a saved HTML file as a web app.

Schema-only; the runner dispatches it to ``POST /v1/artifacts/{id}/publish``.
"""

from __future__ import annotations

from omnigent.superchat.artifacts import ArtifactTool

APPS_TOOL_NAMES = ("artifact_publish",)


class ArtifactPublishTool(ArtifactTool):
    """Publish a saved HTML app; dispatched to ``POST /v1/artifacts/{id}/publish``."""

    _NAME = "artifact_publish"
    _DESC = (
        "Publish a saved single-file HTML app at its own web address. The person is asked to "
        "approve first, including who can open it; only call it when they asked you to publish. "
        "A published app cannot store data yet."
    )
    _PROPERTIES = {
        "artifact_id": {"type": "string", "description": "The artifact id of the HTML file."},
        "audience": {
            "type": "string",
            "enum": ["owner", "org", "link"],
            "description": "Who can open it: only the person, their organization, or anyone "
            "with the link.",
        },
    }
    _REQUIRED = ("artifact_id", "audience")
