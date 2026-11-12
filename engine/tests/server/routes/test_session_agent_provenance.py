"""``_is_tenant_authored``: a fork's clone of an operator template keeps its trust."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from omnigent.server.routes.sessions.routes_agent import _is_tenant_authored


def _agent(agent_id: str, location: str, session_id: str | None) -> Any:
    return SimpleNamespace(id=agent_id, bundle_location=location, session_id=session_id)


class _Store:
    def __init__(self, *agents: Any) -> None:
        self._agents = {a.id: a for a in agents}

    def get(self, agent_id: str) -> Any:
        return self._agents.get(agent_id)


def test_template_is_operator_authored() -> None:
    template = _agent("tpl", "tpl/abc", None)
    assert _is_tenant_authored(_Store(template), template) is False  # type: ignore[arg-type]


def test_fork_clone_of_template_is_operator_authored() -> None:
    template = _agent("tpl", "tpl/abc", None)
    clone = _agent("clone", "tpl/abc", "sess")
    assert _is_tenant_authored(_Store(template, clone), clone) is False  # type: ignore[arg-type]


def test_tenant_upload_is_tenant_authored() -> None:
    template = _agent("tpl", "tpl/abc", None)
    upload = _agent("up", "up/xyz", "sess")
    assert _is_tenant_authored(_Store(template, upload), upload) is True  # type: ignore[arg-type]


def test_clone_keeps_trust_after_template_is_reuploaded() -> None:
    # The template's bundle moved on (new sha) after the clone was made; the
    # clone still points at the old content under the template's prefix.
    template = _agent("tpl", "tpl/new", None)
    clone = _agent("clone", "tpl/old", "sess")
    assert _is_tenant_authored(_Store(template, clone), clone) is False  # type: ignore[arg-type]


def test_prefix_naming_a_session_scoped_agent_is_tenant_authored() -> None:
    other = _agent("other", "other/abc", "sess2")
    forged = _agent("up", "other/abc", "sess")
    assert _is_tenant_authored(_Store(other, forged), forged) is True  # type: ignore[arg-type]


def test_prefix_naming_a_missing_agent_is_tenant_authored() -> None:
    forged = _agent("up", "ghost/abc", "sess")
    assert _is_tenant_authored(_Store(forged), forged) is True  # type: ignore[arg-type]
