"""A message sent while a claude-sdk turn runs reaches that turn (a steer).

The CLI is faked at the SDK client: ``query`` records what is written to its stdin and
``receive_response`` reads what the fake CLI "prints", ending at a ``ResultMessage`` like the
real one. No process and no API call is involved. The real CLI's behaviour these fakes follow
(it echoes a ``priority: "next"`` message with our uuid, both when it attaches it at a tool
boundary and when it runs it as its own next turn) was checked against the bundled CLI.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.harnesses import claude_sdk_executor as module
from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.harnesses.executor import ExecutorError, SteerOutcome, TurnComplete

SESSION = "sess_steer"
#: Printed by the fake CLI to end its stream without a ``ResultMessage`` (the CLI exited).
END = object()


class _Result:
    def __init__(self, text: str, tokens: int = 10, cache_read: int = 0):
        self.session_id = "cli-session"
        self.result = text
        self.is_error = False
        self.usage = {
            "input_tokens": tokens,
            "output_tokens": tokens,
            "cache_read_input_tokens": cache_read,
            "service_tier": "standard",
        }


class _User:
    def __init__(self, uuid: str | None, content: str = ""):
        self.uuid = uuid
        self.content = content
        self.parent_tool_use_id = None


class _Cli:
    """The fake CLI: what it prints is queued; what is written to it is recorded."""

    def __init__(self) -> None:
        self.stdout: asyncio.Queue = asyncio.Queue()
        self.writes: list[dict] = []
        self.prompt_written = asyncio.Event()
        self.fail_writes = False
        self.write_gate: asyncio.Event | None = None
        self.disconnected = 0

    def print_(self, message) -> None:
        self.stdout.put_nowait(message)


def _sdk(cli: _Cli, options_seen: list):
    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = _User
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = _Result
        StreamEvent = type("StreamEvent", (), {})
        HookMatcher = staticmethod(
            lambda matcher=None, hooks=(), **_: SimpleNamespace(hooks=hooks)
        )
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options):
                options_seen.append(options)

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                if cli.fail_writes and not isinstance(prompt, str):
                    raise RuntimeError("stdin closed")
                if isinstance(prompt, str):
                    cli.writes.append({"prompt": prompt})
                    cli.prompt_written.set()
                    return
                if cli.write_gate is not None:
                    await cli.write_gate.wait()
                async for message in prompt:
                    cli.writes.append(message)

            async def receive_response(self):
                while True:
                    message = await cli.stdout.get()
                    if message is END:
                        return
                    yield message
                    if isinstance(message, _Result):
                        return

            async def disconnect(self):
                cli.disconnected += 1

    return _FakeSDK


async def _start_turn(executor: ClaudeSDKExecutor, cli: _Cli, options_seen: list):
    events: list = []

    async def consume() -> None:
        with patch.object(module, "_ensure_sdk", return_value=_sdk(cli, options_seen)):
            async for event in executor.run_turn(
                [{"role": "user", "content": "redo the deck", "session_id": SESSION}], [], ""
            ):
                events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(cli.prompt_written.wait(), 5)
    for _ in range(100):
        if SESSION in executor._steer_windows:
            break
        await asyncio.sleep(0)
    return task, events


def _outcomes(events: list) -> list[tuple[str, bool]]:
    return [(e.steer_id, e.taken) for e in events if isinstance(e, SteerOutcome)]


@pytest.mark.asyncio
async def test_a_message_during_the_turn_is_written_and_taken_when_the_cli_echoes_it():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)

    offer = await executor.steer_session_message(SESSION, "just redo it", steer_id="inj_1")
    assert offer == "pending"  # written, but taken only once the CLI says so

    pushed = cli.writes[-1]
    assert pushed["message"] == {"role": "user", "content": "just redo it"}
    assert pushed["priority"] == "next"  # joins the running turn at its next step
    assert pushed["session_id"] == SESSION
    assert executor._steer_windows[SESSION].pending == {pushed["uuid"]: "inj_1"}

    cli.print_(_User(pushed["uuid"]))  # the CLI attached it at a tool boundary
    cli.print_(_Result("done once"))
    await asyncio.wait_for(task, 5)

    assert _outcomes(events) == [("inj_1", True)]
    assert isinstance(events[-1], TurnComplete) and events[-1].response == "done once"
    assert SESSION not in executor._steer_windows


@pytest.mark.asyncio
async def test_the_cli_is_asked_to_echo_user_messages():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, _ = await _start_turn(executor, cli, seen)
    cli.print_(_Result("ok"))
    await asyncio.wait_for(task, 5)
    assert "replay-user-messages" in seen[0].extra_args


@pytest.mark.asyncio
async def test_a_steer_that_missed_the_turn_is_answered_inside_the_same_turn():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)

    assert await executor.steer_session_message(SESSION, "make it blue", steer_id="b") == "pending"
    uuid = cli.writes[-1]["uuid"]

    # The model was already answering: the turn's result comes first, then the CLI runs the
    # steer as its own next turn. Nothing but the CLI's own output decides when to stop.
    cli.print_(_Result("first answer", tokens=10, cache_read=100))
    await asyncio.sleep(0.05)
    assert not task.done()  # still reading: an answer is owed
    assert executor._steer_windows[SESSION].open

    cli.print_(_User(uuid))
    cli.print_(_Result("blue answer", tokens=5, cache_read=40))
    await asyncio.wait_for(task, 5)

    assert _outcomes(events) == [("b", True)]
    done = events[-1]
    assert isinstance(done, TurnComplete)
    # Both CLI turns are billed, cache buckets included.
    assert done.usage["input_tokens"] == 15
    assert done.usage["output_tokens"] == 15
    assert done.usage["total_tokens"] == 30
    assert done.usage["cache_read_input_tokens"] == 140
    assert done.usage["service_tier"] == "standard"
    assert not executor._steer_windows


@pytest.mark.asyncio
async def test_a_second_steer_during_the_followup_is_read_too():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    await executor.steer_session_message(SESSION, "one", steer_id="s1")
    first = cli.writes[-1]["uuid"]
    cli.print_(_Result("r1"))
    await asyncio.sleep(0.02)
    cli.print_(_User(first))
    await asyncio.sleep(0.02)
    assert await executor.steer_session_message(SESSION, "two", steer_id="s2") == "pending"
    second = cli.writes[-1]["uuid"]
    cli.print_(_Result("r2"))
    await asyncio.sleep(0.02)
    cli.print_(_User(second))
    cli.print_(_Result("r3"))
    await asyncio.wait_for(task, 5)
    assert _outcomes(events) == [("s1", True), ("s2", True)]
    assert sum(isinstance(e, TurnComplete) for e in events) == 1


@pytest.mark.asyncio
async def test_no_steer_is_accepted_once_the_turn_is_over_or_before_it_started():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    assert await executor.steer_session_message(SESSION, "early", steer_id="e") == "refused"

    task, _ = await _start_turn(executor, cli, seen)
    cli.print_(_Result("ok"))
    await asyncio.wait_for(task, 5)

    assert await executor.steer_session_message(SESSION, "late", steer_id="l") == "refused"
    assert [w for w in cli.writes if "uuid" in w] == []  # nothing was written to the CLI


@pytest.mark.asyncio
async def test_a_failed_write_is_refused_and_drops_the_broken_cli():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    cli.fail_writes = True

    assert await executor.steer_session_message(SESSION, "lost?", steer_id="x") == "refused"
    assert not executor._steer_windows[SESSION].pending
    assert SESSION not in executor._clients  # stdin is broken; the next turn starts fresh

    cli.print_(_Result("ok"))
    await asyncio.wait_for(task, 5)
    assert _outcomes(events) == []


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_a_result_refuses_pending_steers_and_drops_the_cli():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    await executor.steer_session_message(SESSION, "ghost", steer_id="g")

    cli.print_(END)  # the CLI went away before answering
    await asyncio.wait_for(task, 5)

    assert _outcomes(events) == [("g", False)]
    assert SESSION not in executor._clients  # the next turn rebuilds; nothing answers it twice
    assert cli.disconnected == 1


@pytest.mark.asyncio
async def test_a_result_with_no_steer_pending_ends_the_turn_at_once():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    cli.print_(_Result("answer"))
    await asyncio.wait_for(task, 5)
    assert [type(e) for e in events] == [TurnComplete]
    assert SESSION in executor._clients  # kept for the next turn


@pytest.mark.asyncio
async def test_a_steer_write_survives_its_caller_being_cancelled():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    cli.write_gate = asyncio.Event()

    offer = asyncio.create_task(executor.steer_session_message(SESSION, "half", steer_id="h"))
    await asyncio.sleep(0.01)
    offer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await offer
    cli.write_gate.set()
    await asyncio.sleep(0.01)

    uuid = cli.writes[-1]["uuid"]  # the write still completed
    assert executor._steer_windows[SESSION].pending == {uuid: "h"}
    cli.print_(_User(uuid))
    cli.print_(_Result("ok"))
    await asyncio.wait_for(task, 5)
    assert _outcomes(events) == [("h", True)]


@pytest.mark.asyncio
async def test_a_cancelled_turn_with_a_steer_in_flight_drops_the_cli():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, _ = await _start_turn(executor, cli, seen)
    await executor.steer_session_message(SESSION, "queued in the cli", steer_id="q")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert SESSION not in executor._clients
    assert SESSION not in executor._steer_windows


def test_the_executor_claims_the_live_queue():
    assert ClaudeSDKExecutor().supports_live_message_queue() is True


def test_usage_sums_numbers_and_nested_buckets_and_keeps_the_latest_text():
    total = module._sum_usage(
        {"input_tokens": 1, "cache_creation": {"ephemeral_5m_input_tokens": 2}, "tier": "a"},
        {"input_tokens": 3, "cache_creation": {"ephemeral_5m_input_tokens": 4}, "tier": "b"},
    )
    assert total == {
        "input_tokens": 4,
        "cache_creation": {"ephemeral_5m_input_tokens": 6},
        "tier": "b",
    }


@pytest.mark.asyncio
async def test_an_error_result_still_ends_the_turn():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    bad = _Result("boom")
    bad.is_error = True
    cli.print_(bad)
    await asyncio.wait_for(task, 5)
    assert isinstance(events[-1], ExecutorError)


@pytest.mark.asyncio
async def test_a_failed_first_answer_is_not_the_turns_end_when_the_steer_turn_succeeds():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    await executor.steer_session_message(SESSION, "try again", steer_id="t")
    uuid = cli.writes[-1]["uuid"]
    bad = _Result("overloaded")
    bad.is_error = True
    cli.print_(bad)
    await asyncio.sleep(0.02)
    cli.print_(_User(uuid))
    cli.print_(_Result("worked this time"))
    await asyncio.wait_for(task, 5)
    assert _outcomes(events) == [("t", True)]
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_a_stream_end_with_a_steer_pending_closes_the_cli_once():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, _ = await _start_turn(executor, cli, seen)
    await executor.steer_session_message(SESSION, "ghost", steer_id="g")
    cli.print_(END)
    await asyncio.wait_for(task, 5)
    assert cli.disconnected == 1


async def _stop_after(options, tool_calls: list[dict]) -> dict:
    """What the turn's ``PostToolBatch`` hook answers the CLI for a batch of calls."""
    (matcher,) = options.hooks["PostToolBatch"]
    return await matcher.hooks[0]({"tool_calls": tool_calls}, None, None)


def _card(name: str = "mcp__omnigent__ask_clarification") -> dict:
    from omnigent.superchat.cards.tools import build_clarification

    card = build_clarification({"question": "Which look?", "options": ["A", "B"]})
    return {"tool_name": name, "tool_response": [{"type": "text", "text": json.dumps(card)}]}


@pytest.mark.asyncio
async def test_a_card_stops_the_cli_after_its_batch_and_nothing_else_does():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, _ = await _start_turn(executor, cli, seen)
    options = seen[0]
    stop = {"continue_": False, "stopReason": "Waiting for the person's answer."}
    assert await _stop_after(options, [_card()]) == stop
    # a sibling call in the batch does not change it: the turn ends after the whole batch
    assert (
        await _stop_after(options, [{"tool_name": "Read", "tool_response": "x"}, _card()]) == stop
    )
    # a refused card lets the model retry
    bad = {"tool_name": "mcp__omnigent__ask_clarification", "tool_response": '{"error": "no"}'}
    assert await _stop_after(options, [bad]) == {}
    # third-party content carrying the key never ends the turn, nor do chips or a Helper start
    page = json.dumps({"ends_turn": True})
    for call in (
        {"tool_name": "mcp__omnigent__suggest_follow_ups", "tool_response": page},
        {"tool_name": "mcp__omnigent__start_helper", "tool_response": page},
        {"tool_name": "WebFetch", "tool_response": page},
        {"tool_name": "Read", "tool_response": [{"type": "text", "text": page}]},
        {"tool_name": "mcp__omnigent__sys_os_read", "tool_response": page},
        {"tool_name": "mcp__github__ask_clarification", "tool_response": page},
        {"tool_name": "mcp__omnigent__ask_clarification",
         "tool_response": json.dumps({"result": page})},
    ):  # fmt: skip
        assert await _stop_after(options, [call]) == {}, call
    cli.print_(_Result("ok"))
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_a_steer_pending_when_the_card_ends_the_turn_is_answered_not_dropped():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    assert await executor.steer_session_message(SESSION, "make it blue", steer_id="b") == "pending"
    uuid = cli.writes[-1]["uuid"]

    # The card's batch resolves: the hook stops the CLI before it attaches the queued steer,
    # so the CLI ends this turn and runs the steer as its next one. Nothing is interrupted.
    assert (await _stop_after(seen[0], [_card()]))["continue_"] is False
    cli.print_(_Result(""))
    await asyncio.sleep(0.05)
    assert not task.done()  # an answer is still owed for the steer
    cli.print_(_User(uuid))
    cli.print_(_Result("blue answer"))
    await asyncio.wait_for(task, 5)

    assert _outcomes(events) == [("b", True)]
    assert isinstance(events[-1], TurnComplete) and events[-1].response == "blue answer"
    assert cli.disconnected == 0


@pytest.mark.asyncio
async def test_a_steer_after_the_card_stop_runs_in_order_and_later_ones_are_refused():
    executor, cli, seen = ClaudeSDKExecutor(), _Cli(), []
    task, events = await _start_turn(executor, cli, seen)
    assert (await _stop_after(seen[0], [_card()]))["continue_"] is False

    # Sent after the stop but before the CLI's result: still read, as the CLI's next turn.
    assert await executor.steer_session_message(SESSION, "also this", steer_id="late") == "pending"
    uuid = cli.writes[-1]["uuid"]
    cli.print_(_Result(""))
    cli.print_(_User(uuid))
    cli.print_(_Result("late answer"))
    await asyncio.wait_for(task, 5)
    assert _outcomes(events) == [("late", True)]
    assert events[-1].response == "late answer"

    # Once the turn is over, a steer is refused: it runs as the next turn instead.
    assert await executor.steer_session_message(SESSION, "after", steer_id="x") == "refused"
