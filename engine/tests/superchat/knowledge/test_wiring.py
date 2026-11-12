"""The feature's wiring: the relays mount, nothing is kept in the engine, citations carry ids."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI

from omnigent.superchat.feature import InstallDeps
from omnigent.superchat.knowledge import FEATURE
from omnigent.superchat.transcript.blocks import _citation_block, _citations


def test_install_mounts_only_session_scoped_relays_and_no_job(tmp_path) -> None:
    app = FastAPI()
    FEATURE.install(  # type: ignore[misc]
        app,
        InstallDeps(
            scheduled_task_store=SimpleNamespace(storage_location=f"sqlite:///{tmp_path}/e.db"),
            conversation_store=object(),
            agent_store=None,
            permission_store=None,
            auth_provider=None,
        ),
    )
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/sessions/{session_id}/knowledge/status" in paths
    assert not any(p.startswith("/v1/knowledge") for p in paths)
    assert FEATURE.jobs is None


def test_search_results_become_citation_chips_by_file_id() -> None:
    output = json.dumps(
        {
            "type": "file_search",
            "results": [
                {
                    "file_id": "f" * 32,
                    "file_name": "a.pdf",
                    "path": "uploads/a.pdf",
                    "page": 2,
                    "thumbnail_url": "x",
                },
                {
                    "file_id": "f" * 32,
                    "artifact_id": "a" * 32,
                    "file_name": "a.pdf",
                    "page": 2,
                    "thumbnail_url": None,
                },
                {"file_id": "e" * 32, "file_name": "b.md", "page": 1},
            ],
        }
    )
    found = _citations(output)
    assert found[0] == {
        "fileId": "f" * 32,
        "path": "uploads/a.pdf",
        "name": "a.pdf",
        "page": 2,
        "hasThumbnail": True,
    }
    assert found[1]["artifactId"] == "a" * 32
    block = _citation_block(found)
    assert block is not None and len(block["card"]["data"]["items"]) == 2
