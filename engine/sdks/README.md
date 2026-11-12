# SDKs

Python packages for integrating with omnigent.

## Structure

```
sdks/
  python-client/           # Headless HTTP/SSE client
    pyproject.toml
    omnigent_client/    # import omnigent_client
```

Claude Code skills for SDK development live under `.claude/skills/`.

## `omnigent_client` — the headless client

Pure HTTP/SSE client. No Rich, no prompt_toolkit, no terminal
dependencies. Use this for:

- Scripts that invoke an agent and collect output.
- Web frontends, Slack bots, test harnesses — anything non-terminal.

Three levels of abstraction are available:

1. **Raw events** — `session.send()` yields typed wire events
   (`ResponseCreated`, `TextDelta`, `ToolCallDone`, etc.). 1:1 with SSE.
2. **Semantic blocks** — `BlockStream` folds events into higher-level
   units (`TextChunk`, `ToolGroup`, `ReasoningBlock`, …). Frameworks
   consuming these don't need to reimplement the stream state machine.
3. **Composable transforms** — `pipe`, `skip_blocks`,
   `skip_intermediate_ends`, `merge_text_across_iterations`, `only_agent`.

### Install

```bash
pip install -e sdks/python-client
```

### Minimal invocation

```python
import asyncio
from omnigent_client import OmnigentClient


async def main():
    async with OmnigentClient(base_url="http://localhost:8080") as client:
        session = client.session(model="archer")
        async for event in session.send("hello"):
            print(event)


asyncio.run(main())
```

### Using semantic blocks (web, Slack, or any custom UI)

```python
from omnigent_client import (
    BlockStream,
    TextChunk,
    ToolGroup,
    ResponseEndBlock,
    pipe,
    skip_intermediate_ends,
)


async def handle(websocket, session, text):
    block_stream = BlockStream()
    async for block in pipe(
        block_stream.stream(session, text),
        skip_intermediate_ends(),
    ):
        match block:
            case TextChunk(text=t):
                await websocket.send_json({"type": "text", "chunk": t})
            case ToolGroup(executions=execs):
                await websocket.send_json(
                    {
                        "type": "tools",
                        "data": [{"name": e.name, "output": e.output} for e in execs],
                    }
                )
            case ResponseEndBlock(status=s):
                await websocket.send_json({"type": "done", "status": s})
```
