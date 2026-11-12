"""Guard: the Docker entrypoint wires the same Super Chat stores as ``omnigent server``.

The hosted image starts through ``deployment/entrypoint.py``, not the CLI. Any store the CLI
passes to ``create_app`` but the entrypoint omits silently disables a feature in production:
without ``objective_store`` the ``/v1/objectives`` routes are never mounted (Goals answer 404),
and without ``memory_service`` long-term memory is off.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


class _Captured(Exception):
    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("create_app called")
        self.kwargs = kwargs


@pytest.fixture
def entrypoint():
    return importlib.import_module("deployment.entrypoint")


def test_entrypoint_passes_objective_and_memory_stores(
    entrypoint, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.server.app as server_app

    def _capture(**kwargs: Any) -> None:
        raise _Captured(kwargs)

    monkeypatch.setattr(server_app, "create_app", _capture)
    db = f"sqlite:///{tmp_path / 'omnigent.db'}"
    entrypoint.run_migrations(db)
    resolved = entrypoint._ResolvedConfig(
        cfg={},
        database_url=db,
        artifact_dir=tmp_path / "artifacts",
        artifact_store_uri=None,
        host="127.0.0.1",
        port=0,
    )
    with pytest.raises(_Captured) as caught:
        entrypoint.build_app(resolved)
    kwargs = caught.value.kwargs
    assert kwargs["objective_store"] is not None
    assert "memory_service" in kwargs
    assert "memory_upkeep_store" in kwargs
    if kwargs["memory_service"] is not None:
        assert kwargs["memory_upkeep_store"] is not None
