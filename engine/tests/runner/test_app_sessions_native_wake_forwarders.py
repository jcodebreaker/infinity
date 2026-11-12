"""Tests for wake-post retries and native transcript forwarder lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app_mod


@dataclass
class _WakePost:
    """
    A single recorded POST made by ``_QueuedResponseServerClient``.

    :param url: The path the wake notice was POSTed to, e.g.
        ``"/v1/sessions/0349c7f62dcaa06b868e9c088c39f062/events"``.
    :param notice: The injected notice text pulled out of the request body.
    :param created_by: Optional attribution actor sent with the wake notice.
    """

    url: str
    notice: str
    created_by: str | None = None


class _QueuedResponseServerClient:
    """
    Omnigent HTTP client stub that returns a fixed queue of real responses.

    A real stub (NOT ``MagicMock``) so that an unexpected attribute access or
    an extra POST beyond the queue fails the test loudly instead of silently
    returning a truthy mock. Each ``post`` pops the next pre-built
    :class:`httpx.Response` (so ``raise_for_status`` runs its real logic —
    a 503 raises, a 200 does not) and records the call for assertions.

    :param responses: Responses to return in order, one per ``post`` call,
        e.g. ``[httpx.Response(503, ...), httpx.Response(200, ...)]``.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """
        Store the response queue and an empty call log.

        :param responses: Responses to return in order, one per POST.
        :returns: None.
        """
        self._responses = list(responses)
        self.calls: list[_WakePost] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        """
        Record the POST and return the next queued response.

        :param url: Target path, e.g. ``"/v1/sessions/b460374fc8e697b296708f52dc9d8179/events"``.
        :param json: Wake-notice request body in the ingest message shape.
        :param timeout: Per-request timeout (recorded only, not enforced).
        :returns: The next pre-built response from the queue.
        :raises AssertionError: If more POSTs are made than responses queued.
        """
        notice = json["data"]["content"][0]["text"]
        created_by = json.get("created_by")
        self.calls.append(
            _WakePost(
                url=url,
                notice=notice,
                created_by=created_by if isinstance(created_by, str) else None,
            )
        )
        assert self._responses, (
            f"Wake POST made {len(self.calls)} call(s) but only "
            f"{len(self.calls) - 1} response(s) were queued — the retry "
            f"loop exceeded its bound."
        )
        return self._responses.pop(0)


def _wake_response(status_code: int, parent_id: str) -> httpx.Response:
    """
    Build a real ``httpx.Response`` for a wake POST to ``parent_id``.

    A request is attached so ``raise_for_status`` can construct a proper
    ``HTTPStatusError`` on non-2xx, matching what httpx does in production.

    :param status_code: HTTP status to simulate, e.g. ``503``.
    :param parent_id: Parent session id used to build the request URL.
    :returns: A response carrying a representative JSON body.
    """
    request = httpx.Request("POST", f"http://test/v1/sessions/{parent_id}/events")
    body = (
        {"error": {"code": "RUNNER_UNAVAILABLE", "message": "runner reconnecting"}}
        if status_code >= 400
        else {"id": "evt_1", "object": "session.event"}
    )
    return httpx.Response(status_code, request=request, json=body)


async def test_wake_post_retries_transient_503_then_succeeds(
    _no_wake_backoff: list[float],
) -> None:
    """
    A transient 503 wake response is retried and the next 200 succeeds.

    Guards the core bug: Omnigent returns a genuine 503 ``RUNNER_UNAVAILABLE``
    *response* (not a transport exception) while the parent's runner tunnel
    reconnects. The wake POST must treat that as a failure and retry, not
    accept it as delivered.
    """
    parent_id = "dd17997e050fc080efac96bc9ec22b55"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id), _wake_response(200, parent_id)]
    )

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Returns True only because the retry re-POSTed after the 503 and got a
    # 200. If the status check were missing, the first 503 would be treated
    # as success and there would be exactly one call with delivered already
    # True — so both the count and the value below pin the fix.
    assert delivered is True
    # Exactly two POSTs: the 503 attempt + the 200 retry. One call would mean
    # the 503 was silently accepted; three would mean it retried past success.
    assert len(client.calls) == 2, (
        f"Expected 2 wake POSTs (503 then 200 retry), got {len(client.calls)}."
    )
    # Both POSTs targeted the parent's events endpoint with the same notice.
    assert client.calls[0].url == f"/v1/sessions/{parent_id}/events"
    assert client.calls[1].notice == "[System: worker completed]"
    # Exactly one backoff slept (between the two attempts) — proves the retry
    # path ran rather than the call being retried zero or two+ times.
    assert len(_no_wake_backoff) == 1, (
        f"Expected one backoff before the single retry, got {_no_wake_backoff}."
    )


async def test_wake_post_carries_dispatch_actor() -> None:
    """A wake notice preserves the collaborator that dispatched the child."""
    parent_id = "5a81ef19fce549929c8f9925c2ee034f"
    client = _QueuedResponseServerClient([_wake_response(200, parent_id)])

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
        created_by="bob@example.com",
    )

    assert delivered is True
    assert len(client.calls) == 1
    assert client.calls[0].created_by == "bob@example.com"


async def test_wake_post_retries_without_actor_when_attribution_is_rejected() -> None:
    """A stale collaborator grant cannot permanently drop a parent wake."""
    parent_id = "32561551610d43b39dc4e394b78ea89d"
    client = _QueuedResponseServerClient(
        [_wake_response(403, parent_id), _wake_response(200, parent_id)]
    )

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
        created_by="bob@example.com",
    )

    assert delivered is True
    assert len(client.calls) == 2
    assert client.calls[0].created_by == "bob@example.com"
    assert client.calls[1].created_by is None


async def test_wake_post_persistent_503_returns_failure(
    _no_wake_backoff: list[float],
) -> None:
    """
    A 503 on every attempt exhausts the retry budget and reports failure.

    This is the regression guard for the silent-strand bug: a 503 must be
    surfaced as a delivery failure (so the caller releases the debounce flag
    and logs), never swallowed as a success.
    """
    parent_id = "a25887ef53cb74bba721c20edf204d10"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id) for _ in range(runner_app_mod._WAKE_POST_MAX_ATTEMPTS)]
    )

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # False = the non-2xx response was treated as a failure. Before the fix
    # this returned (implicitly) success and the wake was considered delivered.
    assert delivered is False
    # Attempted exactly the bounded budget — not once (no retry) and not
    # unbounded. The stub would have asserted on a call past the queue.
    assert len(client.calls) == runner_app_mod._WAKE_POST_MAX_ATTEMPTS, (
        f"Expected {runner_app_mod._WAKE_POST_MAX_ATTEMPTS} attempts on persistent 503, "
        f"got {len(client.calls)}."
    )
    # One backoff fewer than attempts: we don't sleep after the final attempt.
    assert len(_no_wake_backoff) == runner_app_mod._WAKE_POST_MAX_ATTEMPTS - 1, (
        f"Expected {runner_app_mod._WAKE_POST_MAX_ATTEMPTS - 1} backoffs between "
        f"{runner_app_mod._WAKE_POST_MAX_ATTEMPTS} attempts, got {_no_wake_backoff}."
    )


async def test_wake_post_permanent_4xx_not_retried(
    _no_wake_backoff: list[float],
) -> None:
    """
    A permanent 4xx wake rejection fails immediately without retrying.

    A 400 is a client-side rejection that retrying cannot fix, so the loop
    must give up after one attempt rather than burn the whole budget.
    """
    parent_id = "43cc3eccd350fed1b91854b2adf5ec3e"
    client = _QueuedResponseServerClient([_wake_response(400, parent_id)])

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Permanent rejection => failure, no delivery.
    assert delivered is False
    # Exactly one attempt: a permanent 4xx is not retried. Two+ would mean the
    # classifier wrongly treated 400 as transient.
    assert len(client.calls) == 1, (
        f"Expected a single attempt on permanent 400, got {len(client.calls)}."
    )
    # No backoff at all — the loop exited before any sleep.
    assert _no_wake_backoff == []


@pytest.mark.parametrize(
    "status_code,expected_retryable",
    [
        (503, True),  # RUNNER_UNAVAILABLE — the routine reconnect case
        (500, True),  # generic server error
        (429, True),  # rate limit — explicitly transient
        (409, True),  # conflict — explicitly transient
        (400, False),  # bad request — permanent
        (404, False),  # not found — permanent
    ],
)
def test_wake_post_is_retryable_status_classification(
    status_code: int, expected_retryable: bool
) -> None:
    """
    The status classifier retries 5xx + transient 4xx, not permanent 4xx.

    :param status_code: Simulated HTTP status on the wake response.
    :param expected_retryable: Whether that status should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.HTTPStatusError(
        "wake rejected",
        request=request,
        response=httpx.Response(status_code, request=request),
    )
    # Pins which statuses cost a retry vs. fail fast; a wrong verdict here
    # would either waste the budget on permanent errors or give up on a 503.
    assert runner_app_mod._wake_post_is_retryable(exc) is expected_retryable


def test_wake_post_transport_error_is_retryable() -> None:
    """
    A transport-level error (no response) is always retryable.

    A ``ConnectError`` carries no HTTP response — the POST may never have
    reached Omnigent — so the wake should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.ConnectError("connection refused", request=request)
    # True because a transport failure is not a definitive server rejection.
    assert runner_app_mod._wake_post_is_retryable(exc) is True


@dataclass
class _ForwarderRun:
    """
    One spawned transcript-forwarder stub run.

    :param task: The asyncio task executing this run, captured via
        ``asyncio.current_task()`` when the stub body starts. Used for
        registry-independent cleanup.
    :param cancelled: ``True`` once the parked run observed
        :class:`asyncio.CancelledError`.
    """

    task: asyncio.Task[Any] | None = None
    cancelled: bool = False


async def _drain_forwarder_runs(runs: list[_ForwarderRun]) -> None:
    """
    Cancel and await any still-parked forwarder stub runs.

    Test cleanup helper so a failed assertion never leaks a parked task
    (or a registry entry) into the next test.

    :param runs: Stub runs recorded by a parking forwarder fake.
    :returns: None.
    """
    leftovers = [run.task for run in runs if run.task is not None and not run.task.done()]
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers)
