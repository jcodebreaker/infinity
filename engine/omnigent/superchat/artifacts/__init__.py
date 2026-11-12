"""Artifacts: deliverable files the Muse saved from its host, openable in the app.

Layout: ``store`` (table + blobs, versioned by name per Conversation), ``routes``
(``/v1/artifacts``), ``tools`` (tool defs), ``handlers`` (runner side), ``look`` (a saved page
seen in a render), ``writeback`` (hand edits -> the host workspace, run as a turn prefix),
``feature`` (registration).
"""

from __future__ import annotations

from omnigent.superchat.artifacts.feature import ARTIFACTS_FEATURE
from omnigent.superchat.artifacts.handlers import resolve_workspace_file, workspace_roots
from omnigent.superchat.artifacts.routes import (
    artifact_to_response,
    check_id,
    load_artifact,
    producing_turn,
    publication_to_response,
    request_owner,
)
from omnigent.superchat.artifacts.store import (
    AUDIENCES,
    Artifact,
    Publication,
    SqlAlchemyArtifactStore,
    VersionConflictError,
    ViewStats,
)
from omnigent.superchat.artifacts.tools import ArtifactTool

FEATURE = ARTIFACTS_FEATURE

#: The public surface the capabilities built on artifacts (apps, sheets) import.
__all__ = [
    "ARTIFACTS_FEATURE",
    "AUDIENCES",
    "FEATURE",
    "Artifact",
    "ArtifactTool",
    "Publication",
    "SqlAlchemyArtifactStore",
    "VersionConflictError",
    "ViewStats",
    "artifact_to_response",
    "check_id",
    "load_artifact",
    "producing_turn",
    "publication_to_response",
    "request_owner",
    "resolve_workspace_file",
    "workspace_roots",
]
