"""Shared fixtures for omnigent.superchat tests."""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """:returns: A SqlAlchemyConversationStore backed by the test database."""
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture(autouse=True)
def _isolated_knowledge_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point the host's workspace (``memory/``) at a temp folder: a hand edit's journal
    line, like a checkpoint's, must never land in the developer's real home."""
    monkeypatch.setenv("SUPERCHAT_KNOWLEDGE_ROOT", str(tmp_path_factory.mktemp("workspace")))
