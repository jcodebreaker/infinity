"""Tests for WRONG_REPLICA classification in RunnerRouter._runner_absent_code."""

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode
from omnigent.runner.routing import RunnerRouter, routing_host_id


class MockHostRegistry:
    """Mock host registry for testing."""

    def __init__(self, hosts=None):
        self.hosts = hosts or {}

    def get(self, host_id):
        return self.hosts.get(host_id)


class MockHostStore:
    """Mock host store for testing."""

    def __init__(self, online_hosts=None):
        self.online_hosts = online_hosts or {}

    def is_online(self, host_id):
        return self.online_hosts.get(host_id, False)


class MockTunnelRegistry:
    """Mock tunnel registry."""

    def get(self, runner_id):
        return None


class MockConversationStore:
    """Mock conversation store."""

    def __init__(self, *conversations):
        self.rows = {conv.id: conv for conv in conversations}

    def get_conversation(self, session_id):
        return self.rows.get(session_id)

    def get_conversations(self, session_ids):
        return {sid: self.rows[sid] for sid in session_ids if sid in self.rows}


def test_runner_absent_code_no_host_id_returns_runner_unavailable():
    """When no host_id is provided, classify as RUNNER_UNAVAILABLE."""
    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
    )
    code = router._runner_absent_code(None)
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_no_registries_returns_runner_unavailable():
    """When no registries are wired, classify as RUNNER_UNAVAILABLE."""
    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=None,
        host_store=None,
    )
    code = router._runner_absent_code("host_123")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_host_on_this_replica_returns_runner_unavailable():
    """When host is on this replica, it's genuinely unavailable."""
    host_registry = MockHostRegistry({"host_123": "connection_obj"})
    host_store = MockHostStore()

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_123")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_host_absent_locally_but_online_returns_wrong_replica():
    """When host is absent locally but online elsewhere → WRONG_REPLICA."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    host_store = MockHostStore({"host_456": True})  # Online somewhere

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_456")
    assert code == ErrorCode.WRONG_REPLICA


def test_runner_absent_code_host_absent_everywhere_returns_runner_unavailable():
    """When host is absent locally AND offline everywhere → RUNNER_UNAVAILABLE."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    host_store = MockHostStore({})  # Empty: not online anywhere

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_dead")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_no_store_registry_only_returns_wrong_replica():
    """When store is absent but registry says host not here → treat as wrong_replica."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    # No host_store: should fall back to registry-only check

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=None,
    )
    code = router._runner_absent_code("host_789")
    # Without store, absence locally is treated as wrong replica (could be elsewhere)
    assert code == ErrorCode.WRONG_REPLICA


@pytest.mark.parametrize("root_host", [None, "host_root"])
@pytest.mark.parametrize("broken_parent", ["missing", "cycle"])
def test_routing_host_handles_broken_ancestry(root_host, broken_parent):
    root = Conversation(
        id="root", created_at=1, updated_at=1, root_conversation_id="root", host_id=root_host
    )
    child = Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        kind="sub_agent",
        parent_conversation_id="parent",
        root_conversation_id=root.id,
    )
    conversations = [root, child]
    if broken_parent == "cycle":
        conversations.append(
            Conversation(
                id="parent",
                created_at=1,
                updated_at=1,
                kind="sub_agent",
                parent_conversation_id=child.id,
                root_conversation_id=root.id,
            )
        )

    assert routing_host_id(child, MockConversationStore(*conversations)) == root_host
