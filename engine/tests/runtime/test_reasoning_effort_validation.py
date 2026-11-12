from __future__ import annotations

import pytest

from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.harnesses.executor import ExecutorConfig, ExecutorError
from omnigent.llms.adapters.anthropic import _effort_to_budget
from omnigent.llms.errors import PermanentLLMError


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_anthropic_effort_rejects_openai_only_values(effort: str) -> None:
    with pytest.raises(PermanentLLMError, match="not supported by Anthropic"):
        _effort_to_budget(effort, 10000)


@pytest.mark.asyncio
async def test_claude_sdk_rejects_none_before_sdk_call() -> None:
    executor = ClaudeSDKExecutor(gateway=False)
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "none"}),
        )
    ]
    assert any(
        isinstance(e, ExecutorError) and "not supported by Claude" in e.message for e in events
    )
