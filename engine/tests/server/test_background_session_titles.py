from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date

import pytest

from omnigent.server import background_session_titles as titles_module
from omnigent.server.background_session_titles import (
    BACKGROUND_SESSION_TITLES_HEADER,
    BACKGROUND_TITLE_MAX_CHARS,
    CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
    FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION,
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    ProxyBackgroundTitleGenerator,
    background_session_titles_enabled,
    build_background_title_instructions,
    normalize_background_title,
    prepare_background_session_title,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_turn_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(titles_module, "TURN_HOLD_SECONDS", 0.0)


def _seed_session(store: SqlAlchemyConversationStore, title: str) -> str:
    conversation = store.create_conversation(kind="default", title=title)
    return conversation.id


async def test_prepare_background_title_for_eligible_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is not None


async def test_prepare_background_title_skips_when_user_setting_is_disabled(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
        enabled=False,
    )

    assert pending is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, True),
        ({BACKGROUND_SESSION_TITLES_HEADER: "on"}, True),
        ({BACKGROUND_SESSION_TITLES_HEADER: "off"}, False),
    ],
)
async def test_background_session_titles_enabled_defaults_on(
    headers: dict[str, str],
    expected: bool,
) -> None:
    assert background_session_titles_enabled(headers) is expected


async def test_prepare_background_title_from_message(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = uuid.uuid4().hex
    conversation = store.create_conversation(kind="default", agent_id=agent_id)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    pending = prepare_background_session_title(
        coordinator=coordinator,
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "please investigate"},
                    {"type": "input_text", "text": "the authentication timeout"},
                ],
            },
        ),
    )

    assert pending is not None
    assert pending.request == BackgroundTitleRequest(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        agent_id=agent_id,
    )


async def test_slash_command_background_title_uses_persisted_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Review migration plan"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="slash_command",
            data={"kind": "skill", "name": "grill-me", "arguments": "review this plan"},
        ),
    )

    assert pending is not None
    assert pending.request.prompt == "/grill-me review this plan"
    persisted = store.update_conversation(conversation.id, title="grill-me review this plan")
    assert persisted is not None
    pending.schedule(expected_seed_title=persisted.title)
    await pending.coordinator.wait_for_idle()

    assert store.get_conversation(conversation.id).title == "Review migration plan"


@pytest.mark.parametrize("excluded_session", ["titled", "child"])
async def test_prepare_background_title_skips_non_initial_sessions(
    db_uri: str,
    excluded_session: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(kind="default")
    conversation = (
        store.create_conversation(kind="default", title="Existing title")
        if excluded_session == "titled"
        else store.create_conversation(
            kind="sub_agent",
            title="reviewer:auth",
            parent_conversation_id=parent.id,
        )
    )

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is None


async def test_default_seed_wait_allows_slow_native_session_startup(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)

    assert coordinator._seed_wait_seconds == 15.0


async def test_schedule_returns_before_delayed_generator_finishes(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(request: BackgroundTitleRequest) -> str:
        assert request.prompt == "please investigate the authentication timeout"
        assert request.harness_override == "claude-sdk"
        assert request.model_override == "claude-sonnet-4-6"
        assert request.additional_instructions == "Prefix titles with the current date."
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Prefix titles with the current date.",
    )
    started = time.perf_counter()
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="claude-sdk",
        model_override="claude-sonnet-4-6",
    )
    schedule_elapsed = time.perf_counter() - started

    assert schedule_elapsed < 0.05
    assert store.get_conversation(session_id).title == "Investigate authentication timeout"

    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_unsupported_harness_preserves_deterministic_seed(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def unsupported(_request: BackgroundTitleRequest) -> None:
        return None

    coordinator = BackgroundSessionTitleCoordinator(store, unsupported)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="pi",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generated_title_is_normalized_before_rename(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return '  "Debug   authentication timeout."  \nIgnored explanation'

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_title_normalizer_rejects_empty_and_oversized_output() -> None:
    assert normalize_background_title(None) is None
    assert normalize_background_title("   \n  ") is None
    assert normalize_background_title("x" * (BACKGROUND_TITLE_MAX_CHARS + 1)) is None


async def test_custom_title_instructions_allow_longer_output(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Review configurable title limits")
    generated = "x" * CUSTOM_BACKGROUND_TITLE_MAX_CHARS

    async def generator(_request: BackgroundTitleRequest) -> str:
        return generated

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Use a detailed structured title.",
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="review configurable title limits",
        expected_seed_title="Review configurable title limits",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == generated
    assert (
        normalize_background_title(
            "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS + 1),
            max_chars=CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
        )
        is None
    )


async def test_custom_title_instructions_truncate_oversized_output(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Review configurable title limits")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS + 1)

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Use a detailed structured title.",
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="review configurable title limits",
        expected_seed_title="Review configurable title limits",
    )
    await coordinator.wait_for_idle()

    expected = "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS - 1) + "…"
    assert store.get_conversation(session_id).title == expected
    assert len(expected) == CUSTOM_BACKGROUND_TITLE_MAX_CHARS


async def test_manual_rename_wins_background_title_race(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    store.update_conversation(session_id, title="My manual title")
    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "My manual title"


async def test_generator_waits_for_deterministic_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")
    generator_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        generator_started.set()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await asyncio.sleep(0.1)
    assert not generator_started.is_set()

    store.update_conversation(
        conversation.id,
        title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert generator_started.is_set()
    assert store.get_conversation(conversation.id).title == "Debug authentication timeout"


async def test_timeout_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        await asyncio.sleep(1)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        timeout_seconds=0.01,
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generator_failure_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        raise RuntimeError("fake generator failed")

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_schedule_is_one_shot_per_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    calls = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    for _ in range(2):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title="Investigate authentication timeout",
        )
    await coordinator.wait_for_idle()

    assert calls == 1
    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_generation_concurrency_is_bounded(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [
        _seed_session(store, f"Investigate authentication timeout {index}") for index in range(3)
    ]
    release = asyncio.Event()
    running = 0
    peak_running = 0
    two_generators_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_generators_started.set()
        await release.wait()
        running -= 1
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)
    for index, session_id in enumerate(session_ids):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title=f"Investigate authentication timeout {index}",
        )
    await asyncio.wait_for(two_generators_started.wait(), timeout=1.0)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()

    assert all(
        store.get_conversation(session_id).title == "Debug authentication timeout"
        for session_id in session_ids
    )


async def test_seed_polling_is_bounded_by_generation_slots(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [store.create_conversation(kind="default").id for _ in range(3)]
    release = asyncio.Event()
    two_waiters_started = asyncio.Event()
    running = 0
    peak_running = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)

    async def wait_for_seed(*, session_id: str, expected_seed_title: str) -> bool:
        del session_id, expected_seed_title
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_waiters_started.set()
        await release.wait()
        running -= 1
        return False

    coordinator._wait_for_seed = wait_for_seed  # type: ignore[method-assign]
    for session_id in session_ids:
        coordinator.schedule(
            session_id=session_id,
            prompt="hello",
            expected_seed_title="hello",
        )

    await asyncio.wait_for(two_waiters_started.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()


async def test_unusable_model_answer_is_not_retried(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    calls = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal calls
        calls += 1
        return "x"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="p",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert calls == 1
    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_custom_background_title_instructions_include_date_and_keep_guardrails() -> None:
    instructions = build_background_title_instructions(
        "Use the format mon-dd-PR-number-slug.",
        current_date=date(2026, 8, 26),
    )

    assert "The current date is 2026-08-26." in instructions
    assert "Use the format mon-dd-PR-number-slug." in instructions
    assert instructions.count(FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION) == 1
    assert instructions.endswith("Return only the title with no quotes or markdown.")


async def test_default_background_title_follows_user_message_language() -> None:
    instructions = build_background_title_instructions(None)

    assert FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION in instructions
    assert "equally short phrase" in instructions


class _Caller:
    """A fake owner model: records each call and answers with *answer* (or raises it)."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, *, instructions: str, input_text: str) -> str:
        self.calls.append((instructions, input_text))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _proxy(
    store: SqlAlchemyConversationStore, caller: _Caller | None, owners: list[str]
) -> ProxyBackgroundTitleGenerator:
    async def resolve(owner: str) -> _Caller | None:
        owners.append(owner)
        return caller

    return ProxyBackgroundTitleGenerator(store, resolve_caller=resolve)


async def test_proxy_call_writes_the_title_without_a_session(
    db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    before = len(store.list_conversations().data)
    caller = _Caller("Debug authentication timeout")
    owners: list[str] = []
    coordinator = BackgroundSessionTitleCoordinator(store, _proxy(store, caller, owners))

    with caplog.at_level("INFO", logger=titles_module.__name__):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title="Investigate authentication timeout",
        )
        await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"
    assert len(caller.calls) == 1
    instructions, input_text = caller.calls[0]
    assert instructions == build_background_title_instructions(None)
    assert input_text == (
        "<user_message>\nplease investigate the authentication timeout\n</user_message>"
    )
    assert len(owners) == 1
    # One model call, nothing else: no child session, no runner, no harness.
    assert len(store.list_conversations().data) == before
    assert any(
        record.getMessage().startswith(f"title for {session_id} ")
        and record.getMessage().endswith("s via proxy")
        for record in caplog.records
    )


async def test_proxy_failure_keeps_the_tidied_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    caller = _Caller(RuntimeError("upstream 500"))
    coordinator = BackgroundSessionTitleCoordinator(store, _proxy(store, caller, []))

    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert len(caller.calls) == 1
    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_no_model_keeps_the_tidied_title_and_logs_once_per_owner(
    db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    generator = _proxy(store, None, [])
    request = BackgroundTitleRequest(session_id=_seed_session(store, "Hello"), prompt="hello")

    with caplog.at_level("INFO", logger=titles_module.__name__):
        assert await generator(request) is None
        assert await generator(request) is None

    assert sum("no model to call" in record.getMessage() for record in caplog.records) == 1


async def test_title_waits_out_the_first_seconds_of_its_turn(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    called_at: list[float] = []

    async def generator(_request: BackgroundTitleRequest) -> str:
        called_at.append(time.time())
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_hold_seconds=0.2)
    scheduled_at = time.time()
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert called_at and called_at[0] - scheduled_at >= 0.19
    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_coordinator_defaults_bound_title_calls() -> None:
    async def generator(_request: BackgroundTitleRequest) -> str:
        return "x"

    coordinator = BackgroundSessionTitleCoordinator(None, generator)  # type: ignore[arg-type]

    assert coordinator._timeout_seconds == 10.0
    assert coordinator._generation_slots._value == 4
