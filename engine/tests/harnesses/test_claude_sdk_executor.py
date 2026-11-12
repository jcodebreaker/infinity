"""Tests for ClaudeSDKExecutor."""

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.harnesses.claude_sdk_executor import _to_anthropic_content_blocks
from omnigent.harnesses.executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStarted,
    ToolCallStatus,
    ToolInputChunk,
    TurnCancelled,
    TurnComplete,
)


@pytest.fixture(autouse=True)
def _fresh_gateway_vocabularies():
    """Each test reads its own gateway listing: the process-wide cache must not carry over."""
    from omnigent.harnesses import claude_sdk_executor

    claude_sdk_executor._GATEWAY_VOCABULARIES.clear()
    yield
    claude_sdk_executor._GATEWAY_VOCABULARIES.clear()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests: Prompt extraction
# ---------------------------------------------------------------------------


class TestPromptExtraction(unittest.TestCase):
    def _make_executor(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        return ClaudeSDKExecutor()

    def test_resumed_session_uses_last_user_message(self):
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        self.assertEqual(
            executor._build_prompt(messages, resume_session=True),
            "Second question",
        )

    def test_resumed_session_includes_all_trailing_user_messages(self):
        """Batched buffered steers: all trailing user messages must reach the SDK.

        When the runner collapses several buffered steered messages into one
        continuation turn, history ends in >1 consecutive user message the SDK
        has never seen. Sending only the last silently drops the earlier ones
        (the two-steer "second message ignored" bug). All trailing user
        messages must be concatenated into the prompt.
        """
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "steer one"},
            {"role": "user", "content": "steer two"},
        ]
        prompt = executor._build_prompt(messages, resume_session=True)
        self.assertIn("steer one", prompt)
        self.assertIn("steer two", prompt)
        # Prior turns are SDK-cached on resume — not replayed.
        self.assertNotIn("First question", prompt)

    def test_resumed_session_compact_is_a_separate_prompt(self):
        executor = self._make_executor()
        for compact_content in (
            "/compact",
            [{"type": "text", "text": "/compact"}],
            [{"type": "input_text", "text": "/compact"}],
        ):
            with self.subTest(content=compact_content):
                # The runner dispatches pending input before the compact control.
                messages = [
                    {"role": "user", "content": "previous input"},
                    {"role": "user", "content": compact_content},
                ]
                compact_prompt = executor._build_prompt(messages, resume_session=True)
                self.assertEqual(compact_prompt, "/compact")
                messages.extend(
                    [
                        {"role": "user", "content": "next input"},
                        {"role": "user", "content": "another queued input"},
                    ]
                )
                self.assertEqual(
                    executor._build_prompt(messages, resume_session=True),
                    "next input\n\nanother queued input",
                )

    def test_resumed_session_rejects_non_object_content_blocks(self):
        with self.assertRaisesRegex(ValueError, "Anthropic content blocks must be objects"):
            self._make_executor()._build_prompt(
                [{"role": "user", "content": ["/compact"]}], resume_session=True
            )

    def test_resumed_session_trailing_run_stops_at_assistant(self):
        """Only the trailing run of user messages (after the last non-user) is sent."""
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "old one"},
            {"role": "user", "content": "old two"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "new one"},
            {"role": "user", "content": "new two"},
        ]
        prompt = executor._build_prompt(messages, resume_session=True)
        self.assertIn("new one", prompt)
        self.assertIn("new two", prompt)
        self.assertNotIn("old one", prompt)
        self.assertNotIn("old two", prompt)

    def test_resumed_session_multimodal_trailing_run_preserves_blocks(self):
        """A multimodal message in the trailing run keeps structured blocks for all."""
        executor = self._make_executor()
        messages = [
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "describe this"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,aGVsbG8=",
                    },
                    {"type": "input_text", "text": "and this too"},
                ],
            },
        ]
        prompt = executor._build_prompt(messages, resume_session=True)
        self.assertIsInstance(prompt, list)
        types = [b.get("type") for b in prompt]
        self.assertIn("image", types)
        joined = " ".join(b.get("text", "") for b in prompt if b.get("type") == "text")
        self.assertIn("describe this", joined)
        self.assertIn("and this too", joined)
        # The engine's turn start puts an attached image after the message's text, with its
        # name; warm or cold, the model gets it as an image block.
        engine_turn = {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {
                    "type": "input_image",
                    "filename": "Screenshot 4.40.50 PM.png",
                    "image_url": "data:image/png;base64,aGVsbG8=",
                },
            ],
        }
        image = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
        }
        for resume in (True, False):
            with self.subTest(resume_session=resume):
                self.assertEqual(
                    executor._build_prompt([engine_turn], resume_session=resume),
                    [{"type": "text", "text": "what is this?"}, image],
                )

    def test_empty_messages(self):
        executor = self._make_executor()
        self.assertEqual(executor._build_prompt([], resume_session=False), "")

    def test_no_user_messages(self):
        executor = self._make_executor()
        messages = [{"role": "assistant", "content": "Hello"}]
        self.assertEqual(executor._build_prompt(messages, resume_session=False), "")

    def test_dict_content_converted(self):
        executor = self._make_executor()
        messages = [{"role": "user", "content": {"key": "value"}}]
        self.assertIn("key", executor._build_prompt(messages, resume_session=False))

    def test_fresh_session_with_history_serializes_context(self):
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "The secret codeword is ZEBRA-99."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Summarize our conversation."},
        ]
        prompt = executor._build_prompt(messages, resume_session=False)
        self.assertIn("Conversation so far:", prompt)
        self.assertIn("ZEBRA-99", prompt)
        self.assertIn("Summarize our conversation.", prompt)

    def test_history_unresolved_file_id_becomes_visible_marker(self):
        # A prior-turn attachment the content resolver never inlined must
        # not be serialized as raw block JSON — the model reads that as if
        # the attachment were present and hallucinates its content.
        executor = self._make_executor()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "file_id": "file_img", "filename": "photo.png"},
                    {"type": "input_text", "text": "look at this image"},
                ],
            },
            {"role": "assistant", "content": "A photo."},
            {"role": "user", "content": "What did I show you?"},
        ]
        prompt = executor._build_prompt(messages, resume_session=False)
        self.assertIn("[Attachment photo.png could not be loaded]", prompt)
        self.assertNotIn("file_id", prompt)
        self.assertIn("look at this image", prompt)

    def _text_of(self, prompt):
        """Join the text blocks of a structured prompt for framing assertions."""
        return "\n".join(block["text"] for block in prompt if block.get("type") == "text")

    def test_cold_reload_replays_historical_image_as_structured_block(self):
        # A restarted/fresh SDK client must be able to *look at* an image from
        # an earlier turn. Flattening it into a text marker leaves the model
        # describing an attachment it cannot see.
        executor = self._make_executor()
        image_payload = base64.b64encode(b"synthetic png bytes").decode("ascii")
        image_data_uri = f"data:image/png;base64,{image_payload}"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": image_data_uri,
                        "filename": "screenshot.png",
                    },
                    {"type": "input_text", "text": "What is shown here?"},
                ],
            },
            {"role": "assistant", "content": "It shows a test image."},
            {"role": "user", "content": "Summarize our conversation."},
        ]

        prompt = executor._build_prompt(messages, resume_session=False)

        self.assertIsInstance(prompt, list)
        images = [block for block in prompt if block.get("type") == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(
            images[0]["source"],
            {"type": "base64", "media_type": "image/png", "data": image_payload},
        )
        # The bytes travel as a structured block, never as prompt text.
        text = self._text_of(prompt)
        self.assertNotIn(image_payload, text)
        self.assertNotIn("data:", text)
        # Framing and ordering survive around the replayed image.
        self.assertIn("Conversation so far:", text)
        self.assertIn("What is shown here?", text)
        self.assertIn("Respond to the latest user message", text)
        self.assertIn("Summarize our conversation.", text)

    def test_cold_reload_replays_historical_file_as_structured_document(self):
        # The same fidelity requirement applies to non-image attachments: a
        # resolved PDF must reach the SDK as a document block, not a marker.
        executor = self._make_executor()
        file_payload = base64.b64encode(b"synthetic pdf bytes").decode("ascii")
        file_data_uri = f"data:application/pdf;base64,{file_payload}"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "file_data": file_data_uri,
                        "filename": "doc.pdf",
                    },
                    {"type": "input_text", "text": "What does this document say?"},
                ],
            },
            {"role": "assistant", "content": "It is a test document."},
            {"role": "user", "content": "Summarize our conversation."},
        ]

        prompt = executor._build_prompt(messages, resume_session=False)

        self.assertIsInstance(prompt, list)
        documents = [block for block in prompt if block.get("type") == "document"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[0]["source"],
            {"type": "base64", "media_type": "application/pdf", "data": file_payload},
        )
        text = self._text_of(prompt)
        self.assertNotIn(file_payload, text)
        self.assertIn("What does this document say?", text)

    def test_cold_reload_keeps_unresolved_history_attachment_visible(self):
        # A resolved and an unresolved attachment in the same history: the
        # resolved one becomes real bytes, the unresolved one must still say
        # so out loud rather than vanish from the structured prompt.
        executor = self._make_executor()
        image_payload = base64.b64encode(b"synthetic png bytes").decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_payload}",
                        "filename": "resolved.png",
                    },
                    {
                        "type": "input_image",
                        "file_id": "file_missing",
                        "filename": "lost.png",
                    },
                    {"type": "input_text", "text": "two attachments"},
                ],
            },
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": "What did I send?"},
        ]

        prompt = executor._build_prompt(messages, resume_session=False)

        self.assertIsInstance(prompt, list)
        self.assertEqual(len([b for b in prompt if b.get("type") == "image"]), 1)
        text = self._text_of(prompt)
        self.assertIn("[Attachment lost.png could not be loaded]", text)
        self.assertNotIn("file_id", text)
        self.assertIn("two attachments", text)

    def test_historical_image_source_block_is_replaced_with_compact_placeholder(self):
        # The ``Read`` tool returns an image file as an Anthropic content block
        # ``{"type": "image", "source": {"type": "base64", ...}}`` — raw base64
        # with no ``data:`` URI prefix. Redaction must catch this shape too, or a
        # replayed image tool result flattens hundreds of KB into prompt text.
        from omnigent.harnesses.claude_sdk_executor import _render_prior_content_blocks

        image_payload = base64.b64encode(b"synthetic png bytes").decode("ascii")
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_payload,
                },
            }
        ]

        rendered = self._text_of(_render_prior_content_blocks(content))

        self.assertNotIn(image_payload, rendered)
        self.assertIn(
            f"[image: image/png, {len(image_payload)} base64 chars]",
            rendered,
        )


# ---------------------------------------------------------------------------
# Tests: Constructor and properties
# ---------------------------------------------------------------------------


class TestConstructor(unittest.TestCase):
    def test_default_values(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.spec.types import RetryPolicy

        executor = ClaudeSDKExecutor()
        self.assertFalse(executor._os_env)
        self.assertIsNone(executor._os_env_spec)
        self.assertIsNone(executor._cwd)
        self.assertIsNone(executor._model_override)
        self.assertEqual(executor._permission_mode, "auto")
        self.assertIsNone(executor._tool_executor)
        self.assertEqual(executor._clients, {})
        self.assertEqual(executor._crashed_sessions, {})
        self.assertFalse(executor._gateway)
        # _extra_env carries Tool Search plus the default RetryPolicy's CLI env.
        self.assertEqual(
            executor._extra_env,
            {
                "ENABLE_TOOL_SEARCH": "true",
                **RetryPolicy().claude_cli.env(),
            },
        )

    def test_os_env_spec_with_no_sandbox_keeps_native_tools_enabled(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec

        executor = ClaudeSDKExecutor(
            os_env=OSEnvSpec(
                type="caller_process",
                sandbox=OSEnvSandboxSpec(type="none"),
            )
        )
        self.assertTrue(executor._os_env)
        self.assertIsNotNone(executor._os_env_spec)

    def test_os_env_spec_wraps_cli_and_enables_native_tools(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, PreparedClaudeCli
        from omnigent.harnesses.datamodel import OSEnvSpec

        spec = OSEnvSpec(type="caller_process", cwd="/tmp/work")
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor.prepare_claude_cli_path",
                return_value=PreparedClaudeCli(
                    cli_path="/tmp/omnigent-claude-wrapper",
                    enable_native_tools=True,
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(os_env=spec)

        self.assertTrue(executor._os_env)
        self.assertEqual(executor._os_env_spec, spec)
        self.assertEqual(executor._cli_path, "/tmp/omnigent-claude-wrapper")
        self.assertEqual(executor._cwd, "/tmp/work")

    def test_prepare_claude_cli_path_adds_internal_roots_to_read_allowlist(self):
        from omnigent.harnesses.claude_sdk_executor import prepare_claude_cli_path
        from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec
        from omnigent.harnesses.sandbox import SandboxPolicy

        captured: dict[str, SandboxPolicy] = {}

        def _capture_launcher(_path: str, sandbox: SandboxPolicy) -> str:
            captured["sandbox"] = sandbox
            return "/tmp/launcher"

        spec = OSEnvSpec(
            type="caller_process",
            cwd="/tmp/work",
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap",
                read_paths=["."],
                write_paths=["."],
                allow_network=True,
            ),
        )

        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor.resolve_sandbox",
                return_value=SandboxPolicy(
                    backend_type="linux_bwrap",
                    active=True,
                    read_roots=[Path("/tmp/work")],
                    write_roots=[Path("/tmp/work")],
                    write_files=[Path("/dev/null")],
                    allow_network=True,
                ),
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor._claude_internal_write_roots",
                return_value=[Path("/home/test/.claude/sessions")],
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor._claude_internal_write_files",
                return_value=[],
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor.create_exec_launcher",
                side_effect=_capture_launcher,
            ),
        ):
            prepared = prepare_claude_cli_path("/usr/bin/claude", spec)

        self.assertEqual(prepared.cli_path, "/tmp/launcher")
        self.assertTrue(prepared.enable_native_tools)
        # Sandbox helpers call ``.resolve(strict=False)``, which on
        # macOS rewrites ``/home`` → ``/System/Volumes/Data/home``.
        # Compare against the resolved form so the assertion is stable
        # across platforms.
        expected = Path("/home/test/.claude/sessions").resolve(strict=False)
        self.assertIn(expected, captured["sandbox"].read_roots)

    def test_default_process_sandbox_wraps_cli_without_enabling_native_tools(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor.prepare_tight_cli_process_path",
                return_value="/tmp/omnigent-claude-tight-wrapper",
            ),
        ):
            executor = ClaudeSDKExecutor()

        self.assertFalse(executor._os_env)
        self.assertIsNone(executor._os_env_spec)
        self.assertEqual(
            executor._cli_path,
            "/tmp/omnigent-claude-tight-wrapper",
        )

    def test_os_env_spec_without_supported_native_sandbox_disables_native_tools(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, PreparedClaudeCli
        from omnigent.harnesses.datamodel import OSEnvSpec

        spec = OSEnvSpec(type="caller_process", cwd="/tmp/work")
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.harnesses.claude_sdk_executor.prepare_claude_cli_path",
                return_value=PreparedClaudeCli(
                    cli_path="/usr/bin/claude",
                    enable_native_tools=False,
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(os_env=spec)

        self.assertFalse(executor._os_env)
        self.assertEqual(executor._os_env_spec, spec)
        self.assertEqual(executor._cli_path, "/usr/bin/claude")

    def test_model_override(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        executor = ClaudeSDKExecutor(model="claude-haiku-4-5-20251001")
        self.assertEqual(executor._model_override, "claude-haiku-4-5-20251001")

    def test_supports_streaming(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        self.assertTrue(ClaudeSDKExecutor().supports_streaming())

    def test_supports_tool_calling(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        self.assertTrue(ClaudeSDKExecutor().supports_tool_calling())

    def test_databricks_flag_no_creds_raises(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.harnesses.claude_sdk_executor._resolve_gateway_env", return_value={}),
        ):
            with self.assertRaises(EnvironmentError):
                ClaudeSDKExecutor(gateway=True)

    def test_gateway_flag_with_host_override(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with patch.dict("os.environ", {}, clear=True):
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com/",
                base_url_override="https://gateway.example.com/v1",
                gateway_auth_command="printf token",
            )

        self.assertEqual(
            executor._extra_env["ANTHROPIC_BASE_URL"],
            "https://gateway.example.com/v1",
        )
        self.assertEqual(
            executor._extra_env["OMNIGENT_CLAUDE_API_KEY_HELPER"],
            "printf token",
        )
        self.assertEqual(executor._extra_env["ENABLE_TOOL_SEARCH"], "true")

    def test_gateway_model_passes_through(self):
        """Explicit model on the gateway path passes through unchanged."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/v1",
                gateway_auth_command="printf token",
                model="gw-claude-sonnet-4-6",
            )

            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with (
                patch(
                    "omnigent.models.model_catalog.subprocess.run",
                    side_effect=AssertionError("unexpected authentication subprocess"),
                ) as auth_subprocess,
                patch(
                    "omnigent.models.model_catalog.httpx.Client",
                    side_effect=AssertionError("unexpected model-listing HTTP client"),
                ) as http_client,
                patch.object(
                    executor,
                    "_get_or_create_client",
                    side_effect=fake_get_or_create_client,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertEqual(captured["model"], "gw-claude-sonnet-4-6")
            auth_subprocess.assert_not_called()
            http_client.assert_not_called()

        _run(_t())

    def test_databricks_flag_with_host_override_requires_base_url(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_BASE_URL"),
        ):
            ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                gateway_auth_command="printf token",
            )

    def test_databricks_flag_with_host_override_requires_auth_command(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_AUTH_COMMAND"),
        ):
            ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
            )

    def test_databricks_false_no_extra_env(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.spec.types import RetryPolicy

        executor = ClaudeSDKExecutor(gateway=False)
        # gateway=False → no Databricks env, but Tool Search and RetryPolicy
        # CLI env are always merged in.
        self.assertEqual(
            executor._extra_env,
            {
                "ENABLE_TOOL_SEARCH": "true",
                **RetryPolicy().claude_cli.env(),
            },
        )

    def test_neutral_gateway_no_model_does_not_inject_databricks_default(self):
        """Neutral gateway (base URL supplied directly) + no model → ``None``.

        The neutral generic-provider gateway transport never falls back to a
        ``databricks-*`` model: the Omnigent producer resolves a concrete model
        before spawning, so the executor passes ``None`` through to the SDK.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/v1",
                gateway_auth_command="printf token",
            )

            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["model"])

        _run(_t())

    def test_gateway_pins_family_aliases_to_served_ids(self):
        """A gateway turn pins every family alias to an id the gateway serves.

        Claude Code's refusal-fallback resolves the ``opus`` alias through
        ``ANTHROPIC_DEFAULT_OPUS_MODEL``; unpinned it lands on a canonical
        vendor id a gateway serving its own ids rejects (``model_not_found``).
        The executor lists the gateway's models and pins each alias to a
        served id. See ``_apply_gateway_model_vocabulary``.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/anthropic",
                gateway_auth_command="printf token",
                model="gw-claude-fable-5",
            )

            captured: dict[str, dict[str, str]] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["env"] = dict(options.env or {})
                captured["settings"] = json.loads(options.settings or "{}")
                raise RuntimeError("stop after env assembly")

            listing = ModelListing(
                source="openai-compatible",
                verified=True,
                models=(
                    ModelEntry(id="gw-claude-fable-5", family="claude"),
                    ModelEntry(id="gw-claude-opus-4-7", family="claude"),
                    ModelEntry(id="gw-claude-opus-4-8", family="claude"),
                    # A newer Opus alongside 4.8 — the shape that broke the
                    # refusal-fallback: the alias must track the newest while
                    # the rewrites still cover the older generation the CLI's
                    # own route table can name.
                    ModelEntry(id="gw-claude-opus-5", family="claude"),
                    ModelEntry(id="gw-claude-sonnet-5", family="claude"),
                    ModelEntry(id="gw-claude-haiku-4-5", family="claude"),
                    ModelEntry(id="gw-gpt-5-6", family="openai"),
                ),
                note="",
            )
            with (
                patch("omnigent.models.model_catalog.listing_for_provider", return_value=listing),
                patch.object(
                    executor,
                    "_get_or_create_client",
                    side_effect=fake_get_or_create_client,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            env = captured["env"]
            # Newest served generation per family; the non-Claude id is ignored.
            self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "gw-claude-opus-5")
            self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "gw-claude-fable-5")
            self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gw-claude-sonnet-5")
            self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gw-claude-haiku-4-5")
            # Every served Claude generation is reachable by its canonical
            # spelling, so whichever id the CLI's route table names resolves —
            # including the 4.8 the newest-Opus alias no longer points at.
            self.assertEqual(
                captured["settings"]["modelOverrides"],
                {
                    "claude-fable-5": "gw-claude-fable-5",
                    "claude-opus-4-7": "gw-claude-opus-4-7",
                    "claude-opus-4-8": "gw-claude-opus-4-8",
                    "claude-opus-5": "gw-claude-opus-5",
                    "claude-sonnet-5": "gw-claude-sonnet-5",
                    "claude-haiku-4-5": "gw-claude-haiku-4-5",
                },
            )

        _run(_t())

    def test_gateway_listing_without_claude_models_pins_nothing(self):
        """A gateway that lists no Claude models leaves the aliases unpinned.

        The served ids are unknown, so the executor must not invent pins;
        an unpinned alias is today's behavior, not a regression.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/v1",
                gateway_auth_command="printf token",
                model="gpt-5",
            )

            captured: dict[str, dict[str, str]] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["env"] = dict(options.env or {})
                raise RuntimeError("stop after env assembly")

            listing = ModelListing(
                source="openai-compatible",
                verified=True,
                models=(ModelEntry(id="gpt-5", family="openai"),),
                note="",
            )
            with (
                patch("omnigent.models.model_catalog.listing_for_provider", return_value=listing),
                patch.object(
                    executor,
                    "_get_or_create_client",
                    side_effect=fake_get_or_create_client,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertFalse({k for k in captured["env"] if k.startswith("ANTHROPIC_DEFAULT_")})

        _run(_t())

    def test_explicit_family_pins_are_not_overwritten(self):
        """Pre-set ``ANTHROPIC_DEFAULT_*_MODEL`` pins win over the listing.

        A launch config that already pins every alias must be respected
        verbatim. The gateway is still listed, because pinning an alias says
        nothing about how this gateway spells the canonical ids Claude Code
        names on its own — those rewrites are needed either way.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/anthropic",
                gateway_auth_command="printf token",
                model="gw-claude-fable-5",
            )
            executor._extra_env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = "pinned-fable"
            executor._extra_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "pinned-opus"
            executor._extra_env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "pinned-sonnet"
            executor._extra_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "pinned-haiku"

            captured: dict[str, dict[str, str]] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["env"] = dict(options.env or {})
                captured["settings"] = json.loads(options.settings or "{}")
                raise RuntimeError("stop after env assembly")

            listing = ModelListing(
                source="openai-compatible",
                verified=True,
                models=(ModelEntry(id="gw-claude-opus-4-8", family="claude"),),
                note="",
            )
            with (
                patch("omnigent.models.model_catalog.listing_for_provider", return_value=listing),
                patch.object(
                    executor,
                    "_get_or_create_client",
                    side_effect=fake_get_or_create_client,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            for alias in ("FABLE", "OPUS", "SONNET", "HAIKU"):
                self.assertEqual(
                    captured["env"][f"ANTHROPIC_DEFAULT_{alias}_MODEL"],
                    f"pinned-{alias.lower()}",
                )
            self.assertEqual(
                captured["settings"]["modelOverrides"],
                {"claude-opus-4-8": "gw-claude-opus-4-8"},
            )

        _run(_t())

    def test_no_databricks_default_when_databricks_off(self):
        """gateway=False keeps prior behavior: None falls through to the SDK."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(gateway=False)
            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["model"])

        _run(_t())

    def test_no_databricks_leaves_thinking_unset(self):
        """gateway=False does not touch ``thinking``; preserves CLI default."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(gateway=False, model="claude-opus-4-7")
            captured: dict[str, object] = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["thinking"] = getattr(options, "thinking", None)
                raise RuntimeError("stop after options built")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["thinking"])

        _run(_t())

    def test_force_close_client_uses_process_tree_termination(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _Transport:
            def __init__(self):
                self._process = SimpleNamespace(returncode=None, pid=12345, wait=AsyncMock())
                self._stdout_stream = object()
                self._stdin_stream = object()
                self._stderr_stream = object()
                self._stderr_task_group = None
                self._ready = True

        client = SimpleNamespace(_query=None, _transport=_Transport())

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._terminate_process_tree"
            ) as terminate_tree:
                await ClaudeSDKExecutor._force_close_client(client)
            terminate_tree.assert_called_once()

        _run(_t())

    def test_force_close_client_cancels_the_sdk_reader_before_closing_it(self):
        # Regression: marking the query closed while its reader still ran let the reader
        # break out of the transport's message generator mid-yield. The generator's
        # finalizer then raised the CLI's exit status in a task nobody awaited
        # ("Task exception was never retrieved" at close()).
        import gc

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        unretrieved: list[dict] = []

        async def _t():
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: unretrieved.append(context)
            )
            lines: asyncio.Queue = asyncio.Queue()

            async def read_messages():
                try:
                    while True:
                        yield await lines.get()
                except GeneratorExit:
                    pass
                raise RuntimeError("Command failed with exit code 143")

            query = SimpleNamespace(_closed=False)

            async def reader():
                async for _ in read_messages():
                    if query._closed:
                        break

            query._read_task = asyncio.create_task(reader())
            await asyncio.sleep(0)
            lines.put_nowait({"type": "assistant"})  # already on its way at close
            await ClaudeSDKExecutor._force_close_client(
                SimpleNamespace(_query=query, _transport=None)
            )
            await asyncio.gather(query._read_task, return_exceptions=True)
            for _ in range(3):
                gc.collect()
                await asyncio.sleep(0)

        _run(_t())
        gc.collect()

        self.assertEqual(unretrieved, [])

    def test_force_close_client_handles_sdk_without_stderr_task_group(self):
        # Regression: claude-agent-sdk >=0.2.x renamed the stderr reader from an
        # anyio task group (`_stderr_task_group`) to a single `_stderr_task`
        # TaskHandle. A transport shaped like the current SDK (no
        # `_stderr_task_group` attribute at all) must not raise AttributeError
        # out of `_force_close_client` — that exception escaped the runner's
        # lifespan shutdown and crashed it on every session stop.
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        stderr_task = SimpleNamespace(cancel=Mock())

        class _Transport:
            def __init__(self):
                self._process = SimpleNamespace(returncode=None, pid=12345, wait=AsyncMock())
                self._stdout_stream = object()
                self._stdin_stream = object()
                self._stderr_stream = object()
                self._stderr_task = stderr_task  # current SDK shape
                self._ready = True

        transport = _Transport()
        client = SimpleNamespace(_query=None, _transport=transport)

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._terminate_process_tree"
            ) as terminate_tree:
                await ClaudeSDKExecutor._force_close_client(client)
            terminate_tree.assert_called_once()

        _run(_t())

        # New-shape stderr task was cancelled, the missing legacy attribute was
        # never created, and the handle was cleared.
        stderr_task.cancel.assert_called_once()
        self.assertFalse(hasattr(transport, "_stderr_task_group"))
        self.assertIsNone(transport._stderr_task)

    def test_claude_internal_write_files_omits_missing_config(self):
        from omnigent.harnesses.claude_sdk_executor import _claude_internal_write_files

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_path = home / ".claude.json"
            self.assertFalse(config_path.exists())
            with patch(
                "omnigent.harnesses.claude_sdk_executor.pathlib.Path.home", return_value=home
            ):
                paths = _claude_internal_write_files()

            self.assertEqual(paths, [])
            self.assertFalse(config_path.exists())

    def test_claude_internal_write_files_includes_existing_config(self):
        from omnigent.harnesses.claude_sdk_executor import _claude_internal_write_files

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_path = home / ".claude.json"
            config_path.write_text("{}\n", encoding="utf-8")
            with patch(
                "omnigent.harnesses.claude_sdk_executor.pathlib.Path.home", return_value=home
            ):
                paths = _claude_internal_write_files()

            self.assertEqual(paths, [config_path])

    def test_claude_internal_write_files_includes_credentials_when_present(self):
        from omnigent.harnesses.claude_sdk_executor import _claude_internal_write_files

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_path = home / ".claude.json"
            config_path.write_text("{}\n", encoding="utf-8")
            credentials_path = home / ".claude" / ".credentials.json"
            credentials_path.parent.mkdir(parents=True, exist_ok=True)
            credentials_path.write_text("{}\n", encoding="utf-8")
            with patch(
                "omnigent.harnesses.claude_sdk_executor.pathlib.Path.home", return_value=home
            ):
                paths = _claude_internal_write_files()

            self.assertEqual(paths, [config_path, credentials_path])


# ---------------------------------------------------------------------------
# Tests: MCP tool building
# ---------------------------------------------------------------------------


class TestBuildMcpTools(unittest.TestCase):
    def test_builds_tools_from_schemas(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"result": "ok"}

        schemas = [
            {
                "name": "calc",
                "description": "Calculate",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                },
            },
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "calc")

    def test_empty_schemas(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        self.assertEqual(_build_mcp_tools([], None), [])

    def test_handler_calls_executor(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        calls = []

        async def mock_executor(name, args):
            calls.append((name, args))
            return {"result": 42}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertEqual(calls, [("add", {"a": 1})])
        self.assertIn("content", result)
        parsed = json.loads(result["content"][0]["text"])
        self.assertEqual(parsed["result"], 42)
        self.assertNotIn("isError", result)

    def test_handler_marks_blocked_result_as_error(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"blocked": True, "reason": "Exceeded max tool calls"}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertTrue(result["isError"])
        parsed = json.loads(result["content"][0]["text"])
        self.assertTrue(parsed["blocked"])

    def test_handler_marks_error_result_as_error(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"error": "boom"}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertTrue(result["isError"])
        parsed = json.loads(result["content"][0]["text"])
        self.assertEqual(parsed["error"], "boom")

    def test_handler_no_executor(self):
        from omnigent.harnesses.claude_sdk_executor import _build_mcp_tools

        schemas = [
            {
                "name": "x",
                "description": "x",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, None)
        result = _run(tools[0].handler({}))
        parsed = json.loads(result["content"][0]["text"])
        self.assertIn("error", parsed)


# ---------------------------------------------------------------------------
# Tests: Databricks env resolution
# ---------------------------------------------------------------------------


class TestResolveGatewayEnv(unittest.TestCase):
    def test_gateway_keeps_beta_disable(self):
        """A gateway transport disables experimental betas and sets no gateway flag."""
        from omnigent.harnesses.claude_sdk_executor import _resolve_gateway_env

        env = _resolve_gateway_env(
            base_url_override="https://gateway.example.com/v1",
            auth_command_override="printf token",
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.example.com/v1")
        self.assertEqual(env["OMNIGENT_CLAUDE_API_KEY_HELPER"], "printf token")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertNotIn("CLAUDE_CODE_USE_GATEWAY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_no_transport_returns_empty(self):
        from omnigent.harnesses.claude_sdk_executor import _resolve_gateway_env

        self.assertEqual(_resolve_gateway_env(), {})

    def test_generic_provider_gateway_omits_databricks_header(self):
        """Non-Databricks gateways must not receive the Databricks mode header."""
        from omnigent.harnesses.claude_sdk_executor import _resolve_gateway_env

        env = _resolve_gateway_env(
            base_url_override="https://mock-llm.example/v1",
            auth_command_override="printf sk-test",
        )
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)

    def test_host_override_requires_base_url(self):
        from omnigent.harnesses.claude_sdk_executor import _resolve_gateway_env

        with self.assertRaisesRegex(OSError, "GATEWAY_BASE_URL"):
            _resolve_gateway_env(
                host_override="https://example.databricks.com/",
                auth_command_override="printf token",
            )

    def test_host_override_requires_auth_command(self):
        from omnigent.harnesses.claude_sdk_executor import _resolve_gateway_env

        with self.assertRaisesRegex(OSError, "GATEWAY_AUTH_COMMAND"):
            _resolve_gateway_env(
                host_override="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
            )


# ---------------------------------------------------------------------------
# Tests: Gateway model vocabulary (alias pins + canonical rewrites)
# ---------------------------------------------------------------------------


class TestGatewayModelVocabulary(unittest.TestCase):
    def test_pins_map_served_families_to_env_vars(self):
        from omnigent.harnesses.claude_sdk_executor import _gateway_model_vocabulary
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        listing = ModelListing(
            source="openai-compatible",
            verified=True,
            models=(
                ModelEntry(id="databricks-claude-opus-4-8", family="claude"),
                ModelEntry(id="databricks-claude-fable-5", family="claude"),
                ModelEntry(id="databricks-gpt-5-6", family="openai"),
            ),
            note="",
        )
        with patch(
            "omnigent.models.model_catalog.listing_for_provider", return_value=listing
        ) as lister:
            vocabulary = _gateway_model_vocabulary(
                "https://gw.example.com/anthropic", "printf tok"
            )
        self.assertEqual(
            vocabulary.alias_pins,
            {
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "databricks-claude-fable-5",
            },
        )
        # Same listing, second half of the vocabulary: the canonical spelling
        # Claude Code names itself, rewritten to what this gateway serves. The
        # non-Claude id contributes to neither half.
        self.assertEqual(
            vocabulary.model_overrides,
            {
                "claude-opus-4-8": "databricks-claude-opus-4-8",
                "claude-fable-5": "databricks-claude-fable-5",
            },
        )
        # The listing is fetched as a gateway transport: the base URL and the
        # auth command the CLI itself uses, not a Databricks profile.
        provider = lister.call_args.args[0]
        self.assertEqual(provider.base_url, "https://gw.example.com/anthropic")
        self.assertEqual(provider.auth_command, "printf tok")

    def test_nothing_when_the_listing_has_no_claude_models(self):
        from omnigent.harnesses.claude_sdk_executor import (
            _EMPTY_GATEWAY_VOCABULARY,
            _gateway_model_vocabulary,
        )
        from omnigent.models.model_catalog import ModelListing

        listing = ModelListing(source="openai-compatible", verified=True, models=(), note="")
        with patch("omnigent.models.model_catalog.listing_for_provider", return_value=listing):
            self.assertEqual(
                _gateway_model_vocabulary("https://gw.example.com/anthropic", "printf tok"),
                _EMPTY_GATEWAY_VOCABULARY,
            )

    def test_nothing_when_the_listing_fails(self):
        """A failed listing is swallowed — today's behavior is the safe default."""
        from omnigent.harnesses.claude_sdk_executor import (
            _EMPTY_GATEWAY_VOCABULARY,
            _gateway_model_vocabulary,
        )

        with patch(
            "omnigent.models.model_catalog.listing_for_provider",
            side_effect=RuntimeError("listing unavailable"),
        ):
            self.assertEqual(
                _gateway_model_vocabulary("https://gw.example.com/anthropic", "printf tok"),
                _EMPTY_GATEWAY_VOCABULARY,
            )

    def test_direct_endpoint_never_lists_or_pins(self):
        """Off the gateway transport the CLI resolves canonical ids itself."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        executor = ClaudeSDKExecutor()
        env = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
        listing = Mock()
        with patch("omnigent.models.model_catalog.listing_for_provider", listing):
            overrides = _run(executor._apply_gateway_model_vocabulary(env, None))
        self.assertEqual(env, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
        self.assertEqual(overrides, {})
        listing.assert_not_called()

    def test_anthropic_endpoint_through_the_gateway_is_never_listed(self):
        """``api.anthropic.com`` speaks canonical ids: no listing, even on the gateway path."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        executor = ClaudeSDKExecutor.__new__(ClaudeSDKExecutor)
        executor._gateway = True
        env = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
        listing = Mock()
        with patch("omnigent.models.model_catalog.listing_for_provider", listing):
            overrides = _run(executor._apply_gateway_model_vocabulary(env, "printf tok"))
        self.assertEqual(overrides, {})
        listing.assert_not_called()

    def test_gateway_listing_is_read_once_per_process(self):
        """A second executor in the same process reuses the listing instead of re-reading it."""
        from omnigent.harnesses import claude_sdk_executor as module
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        listing = ModelListing(
            source="openai-compatible",
            verified=True,
            models=(ModelEntry(id="gw-claude-opus-4-8", family="claude"),),
            note="",
        )
        reader = Mock(return_value=listing)
        with (
            patch.dict(module._GATEWAY_VOCABULARIES, clear=True),
            patch("omnigent.models.model_catalog.listing_for_provider", reader),
        ):
            for _ in range(2):
                executor = module.ClaudeSDKExecutor.__new__(module.ClaudeSDKExecutor)
                executor._gateway = True
                env = {"ANTHROPIC_BASE_URL": "https://once.example.com/anthropic"}
                overrides = _run(executor._apply_gateway_model_vocabulary(env, None))
                self.assertEqual(overrides, {"claude-opus-4-8": "gw-claude-opus-4-8"})
        self.assertEqual(reader.call_count, 1)

    def test_slow_gateway_listing_blocks_a_launch_for_at_most_a_second(self):
        """A listing slower than the cap leaves the launch unpinned; it lands for the next one."""
        import threading
        import time as time_module

        from omnigent.harnesses import claude_sdk_executor as module

        release = threading.Event()

        def _slow(_base_url, _auth_command):
            release.wait(5)
            return module._EMPTY_GATEWAY_VOCABULARY

        async def _t():
            started = time_module.monotonic()
            vocabulary = await module._cached_gateway_vocabulary(
                "https://slow.example.com/anthropic", None
            )
            waited = time_module.monotonic() - started
            release.set()
            return vocabulary, waited

        with (
            patch.dict(module._GATEWAY_VOCABULARIES, clear=True),
            patch.object(module, "_GATEWAY_LISTING_WAIT_SECONDS", 0.2),
            patch.object(module, "_gateway_model_vocabulary", _slow),
        ):
            vocabulary, waited = _run(_t())
        self.assertEqual(vocabulary, module._EMPTY_GATEWAY_VOCABULARY)
        self.assertLess(waited, 1.0)

    def test_preset_pins_are_respected_but_rewrites_still_apply(self):
        """A launch config that pins aliases still needs the id rewrites.

        Pinning ``opus`` tells Claude Code which model the *alias* means. It
        says nothing about how this gateway spells the canonical ids the CLI
        names on its own, so the rewrites must be derived either way.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.models.model_catalog import ModelEntry, ModelListing

        listing = ModelListing(
            source="openai-compatible",
            verified=True,
            models=(ModelEntry(id="gw-claude-opus-4-8", family="claude"),),
            note="",
        )
        executor = ClaudeSDKExecutor.__new__(ClaudeSDKExecutor)
        executor._gateway = True
        executor._gateway_vocabulary = None
        env = {
            "ANTHROPIC_BASE_URL": "https://gw.example.com/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "gw-claude-opus-5",
        }
        with patch("omnigent.models.model_catalog.listing_for_provider", return_value=listing):
            overrides = _run(executor._apply_gateway_model_vocabulary(env, "printf tok"))
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "gw-claude-opus-5")
        self.assertEqual(overrides, {"claude-opus-4-8": "gw-claude-opus-4-8"})

    def test_settings_payload_carries_helper_and_rewrites(self):
        from omnigent.harnesses.claude_sdk_executor import _claude_settings_payload

        self.assertIsNone(_claude_settings_payload(None, {}))
        self.assertEqual(
            json.loads(_claude_settings_payload("printf tok", {"claude-opus-4-8": "gw-opus"})),
            {"apiKeyHelper": "printf tok", "modelOverrides": {"claude-opus-4-8": "gw-opus"}},
        )
        # No credential helper, but a gateway that still needs the rewrites.
        self.assertEqual(
            json.loads(_claude_settings_payload(None, {"claude-opus-4-8": "gw-opus"})),
            {"modelOverrides": {"claude-opus-4-8": "gw-opus"}},
        )


# ---------------------------------------------------------------------------
# Tests: Empty prompt handling
# ---------------------------------------------------------------------------


class TestEmptyPrompt(unittest.TestCase):
    def test_empty_prompt_yields_turn_complete(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor()
            events = [e async for e in executor.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            # Empty-prompt short-circuit signals "no assistant text this
            # turn" via ``response=None`` rather than an empty string.
            self.assertIsNone(events[0].response)

        _run(_t())


class TestSystemMessages(unittest.TestCase):
    def test_gateway_auth_uses_api_key_helper_settings(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = []

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options.append(options)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    yield _ResultMessage("default", "ok")

                async def disconnect(self):
                    return None

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                executor = ClaudeSDKExecutor(
                    gateway=True,
                    gateway_host="https://host",
                    base_url_override="https://host/anthropic",
                    gateway_auth_command="printf token",
                    gateway_auth_refresh_interval_ms="900000",
                )
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "",
                    )
                ]
            self.assertEqual(len(events), 1)

        _run(_t())

        self.assertEqual(len(captured_options), 1)
        settings = json.loads(captured_options[0].settings)
        self.assertEqual(settings["apiKeyHelper"], "printf token")
        self.assertEqual(
            captured_options[0].env["ANTHROPIC_BASE_URL"],
            "https://host/anthropic",
        )
        self.assertEqual(captured_options[0].env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"], "900000")
        self.assertEqual(captured_options[0].env["ENABLE_TOOL_SEARCH"], "true")
        self.assertNotIn("OMNIGENT_CLAUDE_API_KEY_HELPER", captured_options[0].env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", captured_options[0].env)

    def test_auth_retry_surfaces_executor_error(self):
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )
        from claude_agent_sdk.types import (
            SystemMessage as SDKSystemMessage,
        )

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            ResultMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = SDKSystemMessage
            StreamEvent = SDKStreamEvent
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKSystemMessage(
                    subtype="api_retry",
                    data={
                        "type": "system",
                        "subtype": "api_retry",
                        "attempt": 1,
                        "max_retries": 10,
                        "retry_delay_ms": 500,
                        "error_status": 401,
                        "error": "authentication_failed",
                    },
                )
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "",
                    )
                ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("authentication failed", events[0].message)
            self.assertIn("401", events[0].message)
            # Non-gateway executor should suggest checking CLI login, not databrickscfg
            self.assertIn("claude /status", events[0].message)
            self.assertNotIn("databrickscfg", events[0].message)

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: superside-chat turns off competing Claude Code engine features
# ---------------------------------------------------------------------------


class TestSupersideChatEngineOptions(unittest.TestCase):
    """``cfg.extra["context_mode"] == "superside-chat"`` (see
    ``_executor_adapter.ExecutorAdapter.run_turn`` / ``CreateResponseRequest
    .context_mode``) turns off every Claude Code feature that would
    compete with Omnigent for context/memory/sub-agents ownership. Every
    other session (no ``context_mode``, or ``"rollover"``) is unaffected."""

    @staticmethod
    def _fake_sdk(captured_options: list):
        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options.append(options)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    yield _ResultMessage("default", "ok")

                async def disconnect(self):
                    return None

        return _FakeSDK

    def _run_turn_with_context_mode(self, context_mode: str | None, checklist: bool = False):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.harnesses.executor import ExecutorConfig

        captured_options: list = []

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=self._fake_sdk(captured_options),
            ):
                executor = ClaudeSDKExecutor()
                extra = {"context_mode": context_mode} if context_mode else {}
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "system prompt",
                        config=ExecutorConfig(extra=extra, checklist=checklist),
                    )
                ]
            self.assertEqual(len(events), 1)

        _run(_t())
        self.assertEqual(len(captured_options), 1)
        return captured_options[0]

    def test_superside_chat_disables_auto_compact_auto_memory_and_tool_search(self):
        options = self._run_turn_with_context_mode("superside-chat")
        self.assertEqual(options.env.get("DISABLE_AUTO_COMPACT"), "1")
        self.assertEqual(options.env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
        # Its tool definitions load with the session: no ToolSearch before the first call.
        self.assertEqual(options.env.get("ENABLE_TOOL_SEARCH"), "false")

    def test_superside_chat_disables_filesystem_settings(self):
        options = self._run_turn_with_context_mode("superside-chat")
        self.assertEqual(options.setting_sources, [])

    def test_superside_chat_hides_sdk_bundled_skills(self):
        options = self._run_turn_with_context_mode("superside-chat")
        self.assertEqual(options.skills, [])

    def test_superside_chat_allows_only_bundle_skills(self):
        import tempfile
        from pathlib import Path

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "skills" / "brief").mkdir(parents=True)
            (Path(tmp) / "skills" / "brief" / "SKILL.md").write_text("---\nname: brief\n---\n")
            (Path(tmp) / "skills" / "empty").mkdir()
            ex = ClaudeSDKExecutor(bundle_dir=Path(tmp), agent_name="muse")
            self.assertEqual(ex._bundle_skill_names(), ["muse:brief"])

    def test_superside_chat_disallows_builtin_subagent_tool(self):
        options = self._run_turn_with_context_mode("superside-chat")
        self.assertIn("Agent", options.disallowed_tools)
        self.assertIn("Task", options.disallowed_tools)

    def test_checklist_opts_the_cli_into_its_native_task_tools(self):
        task_tools = ["TaskCreate", "TaskUpdate", "TaskList", "TaskGet"]
        options = self._run_turn_with_context_mode("superside-chat", checklist=True)
        self.assertEqual(options.env.get("CLAUDE_CODE_ENABLE_TODO_TOOLS"), "1")
        self.assertEqual(options.tools, ["Skill", "ToolSearch", *task_tools])
        self.assertEqual(options.allowed_tools, task_tools)

    def test_unset_context_mode_leaves_engine_features_untouched(self):
        options = self._run_turn_with_context_mode(None)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TODO_TOOLS", options.env)
        self.assertEqual(options.tools, ["Skill", "ToolSearch"])
        self.assertNotIn("DISABLE_AUTO_COMPACT", options.env)
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", options.env)
        self.assertEqual(options.env.get("ENABLE_TOOL_SEARCH"), "true")
        self.assertEqual(options.disallowed_tools, [])
        self.assertNotEqual(getattr(options, "setting_sources", "unset-sentinel"), [])

    def test_rollover_context_mode_leaves_engine_features_untouched(self):
        """Only superside-chat gates these — rollover's own CLI-compaction
        story (native CLIs) is unrelated to this SDK-engine adapter."""
        options = self._run_turn_with_context_mode("rollover")
        self.assertNotIn("DISABLE_AUTO_COMPACT", options.env)
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", options.env)
        self.assertEqual(options.disallowed_tools, [])


# ---------------------------------------------------------------------------
# Tests: skills_filter → SDK skills option translation + plugin manifest
# ---------------------------------------------------------------------------


class TestSkillsFilterTranslation(unittest.TestCase):
    """
    Pin the mapping from the spec's ``skills_filter`` to the
    Claude Agent SDK's ``skills`` option.

    The filter has three meaningful values; getting any of them
    wrong silently changes which user / project / bundled skills
    the model sees. This is the seam between AGENTSPEC.md's YAML
    ``skills:`` field and the SDK's documented ``skills`` knob.
    """

    def test_all_lets_sdk_default_setting_sources(self) -> None:
        """
        ``"all"`` → SDK ``skills="all"`` and
        ``setting_sources=None`` (the SDK's default-derivation
        kicks in, producing ``["user", "project"]``).

        Claim: setting_sources is ``None`` (not ``[]``), letting
        the SDK fill it in. A regression that hardcoded an
        explicit list here would freeze the default and miss
        future SDK changes.
        """
        from omnigent.harnesses.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option("all")
        assert result is not None
        self.assertEqual(result.skills, "all")
        self.assertIsNone(result.setting_sources)

    def test_none_zeros_skills_and_setting_sources(self) -> None:
        """
        ``"none"`` → SDK ``skills=[]`` AND
        ``setting_sources=[]``.

        BOTH must be set to truly suppress host skills. The SDK's
        ``_apply_skills_defaults`` auto-fills
        ``setting_sources=["user","project"]`` when ``skills`` is
        non-None — including when ``skills=[]``. That auto-default
        loads ``~/.claude/skills/`` into the system prompt
        listing even though the ``Skill`` tool itself is hidden.
        Forcing ``setting_sources=[]`` is what actually keeps the
        listing empty. The user-reported regression: with only
        ``skills=[]`` set, ``skills: none`` in YAML still showed
        every host skill in the model's output.
        """
        from omnigent.harnesses.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option("none")
        assert result is not None
        self.assertEqual(result.skills, [])
        self.assertEqual(result.setting_sources, [])

    def test_list_lets_sdk_default_setting_sources(self) -> None:
        """A list of names round-trips and uses the SDK default."""
        from omnigent.harnesses.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option(["foo", "bar:baz"])
        assert result is not None
        self.assertEqual(result.skills, ["foo", "bar:baz"])
        self.assertIsNone(result.setting_sources)

    def test_unknown_string_returns_none_for_caller_fallback(self) -> None:
        """
        Unknown strings (e.g. malformed config bypass) return
        ``None`` so the caller can fall back to ``"all"``. The
        spec parser already validates, so this is a belt-and-
        suspenders defense at the executor boundary.
        """
        from omnigent.harnesses.claude_sdk_executor import _resolve_skills_option

        self.assertIsNone(_resolve_skills_option("bogus"))


# ---------------------------------------------------------------------------
# Tests: StreamEvent-based streaming
# ---------------------------------------------------------------------------


class TestStreamEventStreaming(unittest.TestCase):
    def test_live_clients_are_reused_per_omnigent_session(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        query_calls = []
        connect_calls = []

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    connect_calls.append(self)
                    return

                async def query(self, prompt, session_id="default"):
                    query_calls.append(
                        {
                            "prompt": prompt,
                            "session_id": session_id,
                            "extra_args": getattr(self.options, "extra_args", {}),
                            "tools": getattr(self.options, "tools", None),
                            "allowed_tools": getattr(self.options, "allowed_tools", None),
                            "env": getattr(self.options, "env", None),
                        }
                    )
                    result_session_id = (
                        session_id
                        if str(session_id).startswith("claude-")
                        else f"claude-{session_id}"
                    )
                    _FakeSDK.messages = [_ResultMessage(result_session_id, f"result for {prompt}")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                session_a = [{"role": "user", "content": "hello", "session_id": "session-a"}]
                session_b = [{"role": "user", "content": "bonjour", "session_id": "session-b"}]

                events_a1 = [e async for e in executor.run_turn(session_a, [], "")]
                events_b1 = [e async for e in executor.run_turn(session_b, [], "")]
                events_a2 = [e async for e in executor.run_turn(session_a, [], "")]

            self.assertEqual(query_calls[0]["session_id"], "session-a")
            # ``--bare`` was previously included to suppress host config
            # leakage. It was dropped because bare mode also kills
            # CLAUDE.md auto-discovery, plugin sync, and skill loading —
            # which the spec's ``skills:`` field is the proper knob for.
            # ``--name`` names the session, so the CLI makes no title call of its own.
            self.assertEqual(
                query_calls[0]["extra_args"],
                {"no-session-persistence": None, "replay-user-messages": None, "name": "omnigent"},
            )
            # Skill remains available for configured skills; ToolSearch
            # keeps large MCP definitions deferred until they are needed.
            self.assertEqual(query_calls[0]["tools"], ["Skill", "ToolSearch"])
            self.assertEqual(query_calls[0]["env"]["ENABLE_TOOL_SEARCH"], "true")
            self.assertEqual(query_calls[0]["allowed_tools"], [])
            self.assertEqual(query_calls[1]["session_id"], "session-b")
            self.assertEqual(query_calls[2]["session_id"], "session-a")
            self.assertEqual(len(connect_calls), 2)
            self.assertEqual(len(executor._clients), 2)
            self.assertEqual(events_a1[-1].response, "result for hello")
            self.assertEqual(events_b1[-1].response, "result for bonjour")
            self.assertEqual(events_a2[-1].response, "result for hello")

        _run(_t())

    def test_os_env_spec_exposes_only_explicit_native_tools(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["tools"] = getattr(options, "tools", None)
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            # Explicit ``sandbox=none`` so the test runs on any
            # platform. The default ``OSEnvSandboxSpec.type`` is
            # ``"linux_bwrap"``; without this override,
            # ``resolve_sandbox`` calls into the bwrap backend and
            # raises on macOS even though the test isn't exercising
            # sandbox behavior.
            executor = ClaudeSDKExecutor(
                os_env=OSEnvSpec(
                    type="caller_process",
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sleep",
                                "description": "sleep",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "",
                    )
                ]
            # OS operations route through sys_os_* MCP tools, not SDK
            # built-ins. Skill and ToolSearch do not widen OS access.
            self.assertEqual(captured_options["tools"], ["Skill", "ToolSearch"])
            self.assertIn("mcp__omnigent__sleep", captured_options["allowed_tools"])
            self.assertNotIn("Bash", captured_options["allowed_tools"])
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_mcp_only_session_keeps_discovery_tools_without_native_os_tools(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["tools"] = getattr(options, "tools", None)
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sleep",
                                "description": "sleep",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "",
                    )
                ]
            # Default ``skills_filter="all"`` exposes the ``Skill`` tool
            # through the Claude SDK plugin mechanism. ToolSearch defers
            # MCP definitions until needed. The OS tools
            # (Bash/Read/Edit/Write/Glob/Grep) stay absent — that's
            # what this test pins. Neither base tool widens the FS attack
            # surface.
            self.assertEqual(captured_options["tools"], ["Skill", "ToolSearch"])
            self.assertEqual(captured_options["allowed_tools"], ["mcp__omnigent__sleep"])
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_session_send_tool_is_exposed_via_mcp(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)
                    captured_options["system_prompt"] = getattr(options, "system_prompt", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sys_session_send",
                                "description": "Send into session",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "tool": {"type": "string"},
                                        "session": {"type": "string"},
                                        "args": {"type": "object"},
                                    },
                                },
                            }
                        ],
                        "Delegate through `sys_session_send`.",
                    )
                ]
            self.assertIn("mcp__omnigent__sys_session_send", captured_options["allowed_tools"])
            self.assertIn(
                "use `mcp__omnigent__sys_session_send` when instructions say `sys_session_send`",
                captured_options["system_prompt"],
            )
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_session_rename_tool_uses_exact_sdk_mcp_name(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)
                    captured_options["system_prompt"] = getattr(options, "system_prompt", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sys_session_rename",
                                "description": "Rename current session",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"title": {"type": "string"}},
                                    "required": ["title"],
                                },
                            }
                        ],
                        "Call `sys_session_rename` before replying.",
                    )
                ]
            self.assertIn(
                "mcp__omnigent__sys_session_rename",
                captured_options["allowed_tools"],
            )
            self.assertIn(
                "use `mcp__omnigent__sys_session_rename` when instructions say "
                "`sys_session_rename`",
                captured_options["system_prompt"],
            )
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_crashed_session_refuses_future_turns(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = type("ResultMessage", (), {})
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    raise RuntimeError("claude subprocess crashed")

                async def receive_response(self):
                    if False:
                        yield None

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            messages = [{"role": "user", "content": "hello", "session_id": "session-a"}]
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                first_events = [e async for e in executor.run_turn(messages, [], "")]
                second_events = [e async for e in executor.run_turn(messages, [], "")]

            self.assertIsInstance(first_events[0], ExecutorError)
            self.assertIn("claude subprocess crashed", first_events[0].message)
            self.assertIsInstance(second_events[0], ExecutorError)
            self.assertIn("cannot continue in this Session", second_events[0].message)
            self.assertIn("claude subprocess crashed", second_events[0].message)
            self.assertIn("session-a", executor._crashed_sessions)

        _run(_t())

    def test_cancelled_turn_evicts_wedged_client(self):
        """A cancelled turn evicts the cached client so resume can recover (#2109).

        ``asyncio.CancelledError`` is a ``BaseException``, so the idle-watchdog
        cancel bypasses run_turn's ``except Exception`` boundary and the
        wedged client used to stay cached.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        created = []
        wedge_reached = asyncio.Event()

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options
                    self.index = len(created)
                    created.append(self)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    if self.index == 0:
                        # First client wedges like a stuck tool: no events.
                        wedge_reached.set()
                        await asyncio.Event().wait()
                    yield _ResultMessage("claude-session-a", "recovered")

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            messages = [{"role": "user", "content": "hello", "session_id": "session-a"}]
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):

                async def _consume():
                    return [e async for e in executor.run_turn(messages, [], "")]

                turn = asyncio.ensure_future(_consume())
                await asyncio.wait_for(wedge_reached.wait(), timeout=5)
                turn.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn

                # Evicted without a crash mark so the next turn rebuilds
                # a fresh client.
                self.assertEqual(executor._clients, {})
                self.assertNotIn("session-a", executor._crashed_sessions)

                events = [e async for e in executor.run_turn(messages, [], "")]

            self.assertEqual(len(created), 2)
            self.assertIsInstance(events[-1], TurnComplete)
            self.assertEqual(events[-1].response, "recovered")

        _run(_t())

    def test_an_interrupted_turn_ends_cancelled_not_crashed(self):
        """An interrupt closes the CLI under the turn's read: the turn reports TurnCancelled
        and the session is not marked crashed, so the next turn starts a fresh CLI."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        reading = asyncio.Event()

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = type("ResultMessage", (), {})
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.interrupted = asyncio.Event()

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def interrupt(self):
                    self.interrupted.set()

                async def receive_response(self):
                    reading.set()
                    await self.interrupted.wait()
                    raise RuntimeError("Command failed with exit code 143")
                    yield  # pragma: no cover

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            messages = [{"role": "user", "content": "hello", "session_id": "session-a"}]
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):

                async def _consume():
                    return [e async for e in executor.run_turn(messages, [], "")]

                turn = asyncio.ensure_future(_consume())
                await asyncio.wait_for(reading.wait(), timeout=5)
                self.assertTrue(await executor.interrupt_session("session-a"))
                events = await asyncio.wait_for(turn, timeout=5)

            self.assertEqual(events, [TurnCancelled()])
            self.assertNotIn("session-a", executor._crashed_sessions)
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_close_session_disconnects_live_client(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        disconnect = AsyncMock()
        client = type("Client", (), {"disconnect": disconnect})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)
            await executor.close_session("session-a")
            disconnect.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_interrupt_session_interrupts_then_drops_session(self):
        """A user interrupt fires a safe interrupt, then drops the session.

        ``run_turn`` decides whether to resume via
        ``session_key in self._clients``. Dropping the session after an
        interrupt is what forces the next turn to rebuild from full
        history (with the runner's ``[System: interrupted]`` marker)
        instead of resuming a live session whose transcript still holds
        the abandoned prompt. If ``_clients`` were left populated, the
        next turn would resume and silently continue the canceled
        instruction.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        interrupt = AsyncMock()
        disconnect = AsyncMock()
        client = type("Client", (), {"interrupt": interrupt, "disconnect": disconnect})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)

            interrupted = await executor.interrupt_session("session-a")

            self.assertTrue(interrupted)
            # Safe interrupt is attempted first to halt the in-flight turn.
            interrupt.assert_awaited_once_with()
            # Then the session is torn down (disconnect) and removed from
            # _clients so the next turn cannot resume it. An empty dict here
            # is the invariant that prevents the canceled-instruction leak.
            disconnect.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_interrupt_session_closes_even_when_interrupt_fails(self):
        """A failed safe interrupt still drops the session.

        The session must be torn down regardless of whether the SDK's
        ``interrupt()`` succeeds — otherwise a flaky interrupt would
        leave the abandoned-prompt session resumable. If ``close_session``
        is not awaited here, the interrupt-failure path leaks the session.
        """
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        async def fail_interrupt():
            raise RuntimeError("boom")

        client = type("Client", (), {"interrupt": fail_interrupt})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)
            with patch.object(executor, "close_session", new=AsyncMock()) as close_session:
                interrupted = await executor.interrupt_session("session-a")

            self.assertTrue(interrupted)
            close_session.assert_awaited_once_with("session-a")

        _run(_t())

    def test_close_disconnects_all_live_clients(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        disconnect_a = AsyncMock()
        disconnect_b = AsyncMock()
        client_a = type("Client", (), {"disconnect": disconnect_a})()
        client_b = type("Client", (), {"disconnect": disconnect_b})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client_a, model=None)
            executor._clients["session-b"] = _ClaudeClientState(client=client_b, model=None)
            await executor.close()
            disconnect_a.assert_awaited_once_with()
            disconnect_b.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_close_session_force_closes_on_loop_mismatch(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        client = type("Client", (), {})()
        client.disconnect = AsyncMock()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(
                client=client,
                model=None,
                loop=object(),
                task=None,
            )
            with patch.object(executor, "_force_close_client", new=AsyncMock()) as force_close:
                await executor.close_session("session-a")
            client.disconnect.assert_not_awaited()
            force_close.assert_awaited_once_with(client)
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_text_deltas_yield_text_chunks(self):
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            ResultMessage as SDKResultMessage,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = _Sentinel
            StreamEvent = SDKStreamEvent
            ResultMessage = SDKResultMessage
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKStreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hello"},
                    },
                ),
                SDKStreamEvent(
                    uuid="u2",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": " world"},
                    },
                ),
                SDKResultMessage(
                    subtype="result",
                    session_id="s1",
                    result="Hello world",
                    total_cost_usd=0.0,
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                ),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi"}],
                        [],
                        "",
                    )
                ]
            chunks = [e for e in events if isinstance(e, TextChunk)]
            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[0].text, "Hello")
            self.assertEqual(chunks[1].text, " world")
            self.assertIsInstance(events[-1], TurnComplete)
            self.assertEqual(events[-1].response, "Hello world")

        _run(_t())

    def test_second_text_block_is_separated_from_first(self):
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            ResultMessage as SDKResultMessage,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = _Sentinel
            StreamEvent = SDKStreamEvent
            ResultMessage = SDKResultMessage
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKStreamEvent(
                    uuid="u0",
                    session_id="s1",
                    event={
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                SDKStreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Context-Free Title Request"},
                    },
                ),
                SDKStreamEvent(
                    uuid="u2",
                    session_id="s1",
                    event={
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                SDKStreamEvent(
                    uuid="u3",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "text_delta", "text": "Unclear Question"},
                    },
                ),
                SDKResultMessage(
                    subtype="result",
                    session_id="s1",
                    result="Hello world",
                    total_cost_usd=0.0,
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                ),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi"}],
                        [],
                        "",
                    )
                ]
            chunks = [e for e in events if isinstance(e, TextChunk)]
            joined = "".join(c.text for c in chunks)
            self.assertEqual(joined, "Context-Free Title Request\n\nUnclear Question")
            self.assertEqual(joined.split("\n\n")[-1], "Unclear Question")
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_tool_use_assembly_yields_tool_call_request_with_args(self):
        """
        A streaming turn that contains a ``content_block_start`` tool_use
        event plus a subsequent ``AssistantMessage`` with the assembled
        ``ToolUseBlock.input`` must yield exactly one
        :class:`ToolCallRequest` carrying the assembled args.

        Regression pin: the streaming path used to emit
        ``ToolCallRequest(name=..., args={})`` at ``content_block_start``,
        before Claude had streamed the ``input_json_delta`` events. The
        REPL then rendered tool calls as ``⏵ Bash()`` with no visible
        command because the args summary saw an empty dict. The fix
        defers the emission to the ``AssistantMessage`` branch where
        ``tool_block.input`` is populated.

        The test fails pre-fix at the args assertion below:
        ``reqs[0].args`` is ``{}``, not ``{"command": "ls -la"}``.
        """
        # Real types from the SDK so the production
        # ``isinstance(message, sdk.AssistantMessage)`` and
        # ``isinstance(block, sdk.ToolUseBlock)`` checks fire. A
        # MagicMock / _Sentinel would silently bypass both branches
        # and the test would pass vacuously with zero emissions.
        from claude_agent_sdk.types import (
            AssistantMessage as SDKAssistantMessage,
        )
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            ResultMessage as SDKResultMessage,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )
        from claude_agent_sdk.types import (
            ToolUseBlock as SDKToolUseBlock,
        )

        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = SDKAssistantMessage
            UserMessage = _Sentinel
            SystemMessage = _Sentinel
            StreamEvent = SDKStreamEvent
            ResultMessage = SDKResultMessage
            ClaudeAgentOptions = SDKClaudeAgentOptions
            TextBlock = _Sentinel
            ThinkingBlock = _Sentinel
            ToolUseBlock = SDKToolUseBlock
            ToolResultBlock = _Sentinel
            messages = [
                # Live SDK order: tool_use block_start fires before
                # Claude has streamed the full ``input_json_delta``
                # sequence. The production path now only tracks the
                # tool id here — it does NOT emit ToolCallRequest
                # yet. Keeping this event in the fixture ensures a
                # future regression that re-introduces the early
                # emission fails the ``len(reqs) == 1`` check below.
                SDKStreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "Bash",
                            "input": {},
                        },
                    },
                    # Made inside another call (a sub-agent's): the events say which.
                    parent_tool_use_id="agent_1",
                ),
                *(
                    SDKStreamEvent(
                        uuid=f"d{i}",
                        session_id="s1",
                        event={
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": piece},
                        },
                        parent_tool_use_id="agent_1",
                    )
                    for i, piece in enumerate(['{"command": ', '"ls -la"}'])
                ),
                # Full AssistantMessage after input deltas have
                # assembled — this is where ``tool_block.input`` is
                # populated and the ToolCallRequest must fire.
                SDKAssistantMessage(
                    content=[
                        SDKToolUseBlock(
                            id="tool_1",
                            name="Bash",
                            input={"command": "ls -la"},
                        ),
                    ],
                    model="claude-sonnet-4",
                    parent_tool_use_id="agent_1",
                ),
                SDKResultMessage(
                    subtype="result",
                    session_id="s1",
                    result="",
                    total_cost_usd=0.0,
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                ),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run ls"}],
                        [],
                        "",
                    )
                ]
            reqs = [e for e in events if isinstance(e, ToolCallRequest)]
            # 1 = exactly one emission. A value of 2 would mean the
            # early (empty-args) emission was re-introduced alongside
            # the AssistantMessage one; 0 would mean the new path
            # silently dropped the emission.
            self.assertEqual(len(reqs), 1)
            self.assertEqual(reqs[0].name, "Bash")
            # The specific regression: args must be the assembled dict
            # from ``tool_block.input``, not ``{}``. Pre-fix this was
            # ``{}`` and downstream rendered ``⏵ Bash()``.
            self.assertEqual(reqs[0].args, {"command": "ls -la"})
            call = {"call_id": "tool_1", "parent_tool_use_id": "agent_1"}
            self.assertEqual(reqs[0].metadata, call)
            # The early, arg-less signal is its own event, paired by call_id.
            starts = [e for e in events if isinstance(e, ToolCallStarted)]
            self.assertEqual(starts, [ToolCallStarted(name="Bash", metadata=call)])
            chunks = [e for e in events if isinstance(e, ToolInputChunk)]
            self.assertEqual("".join(c.delta for c in chunks), '{"command": "ls -la"}')
            self.assertTrue(all(c.metadata == {"call_id": "tool_1"} for c in chunks))
            self.assertLess(events.index(starts[0]), events.index(chunks[0]))
            self.assertLess(events.index(chunks[-1]), events.index(reqs[0]))

        _run(_t())

    def test_tool_result_error_yields_tool_call_complete_error(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _ToolUseBlock:
            def __init__(self, id, name, input):
                self.id = id
                self.name = name
                self.input = input

        class _ToolResultBlock:
            def __init__(self, tool_use_id, content, is_error):
                self.tool_use_id = tool_use_id
                self.content = content
                self.is_error = is_error

        class _AssistantMessage:
            def __init__(self, content):
                self.content = content

        class _UserMessage:
            def __init__(self, content):
                self.content = content
                self.parent_tool_use_id = "agent_1"

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = _AssistantMessage
            UserMessage = _UserMessage
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            ToolUseBlock = _ToolUseBlock
            ToolResultBlock = _ToolResultBlock
            TextBlock = type("TextBlock", (), {})
            ThinkingBlock = type("ThinkingBlock", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = [
                _AssistantMessage([_ToolUseBlock("tool_1", "fail", {})]),
                _UserMessage(
                    [
                        _ToolResultBlock(
                            "tool_1",
                            [{"type": "text", "text": '{"error": "boom"}'}],
                            True,
                        )
                    ]
                ),
                _ResultMessage("s1", "done"),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run fail"}],
                        [],
                        "",
                    )
                ]
            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.ERROR)
            self.assertEqual(tool_events[0].error, "boom")
            self.assertEqual(
                tool_events[0].metadata, {"call_id": "tool_1", "parent_tool_use_id": "agent_1"}
            )

        _run(_t())

    def test_tool_result_blocked_yields_blocked_status(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        class _ToolUseBlock:
            def __init__(self, id, name, input):
                self.id = id
                self.name = name
                self.input = input

        class _ToolResultBlock:
            def __init__(self, tool_use_id, content, is_error):
                self.tool_use_id = tool_use_id
                self.content = content
                self.is_error = is_error

        class _AssistantMessage:
            def __init__(self, content):
                self.content = content

        class _UserMessage:
            def __init__(self, content):
                self.content = content

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = _AssistantMessage
            UserMessage = _UserMessage
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            ToolUseBlock = _ToolUseBlock
            ToolResultBlock = _ToolResultBlock
            TextBlock = type("TextBlock", (), {})
            ThinkingBlock = type("ThinkingBlock", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = [
                _AssistantMessage([_ToolUseBlock("tool_1", "blocked_tool", {})]),
                _UserMessage(
                    [
                        _ToolResultBlock(
                            "tool_1",
                            [{"type": "text", "text": '{"blocked": true, "reason": "Nope"}'}],
                            True,
                        )
                    ]
                ),
                _ResultMessage("s1", "done"),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run blocked"}],
                        [],
                        "",
                    )
                ]
            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.BLOCKED)
            self.assertEqual(tool_events[0].error, "Nope")

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: _unset_env_var context manager
# ---------------------------------------------------------------------------


def test_unset_env_var_removes_and_restores(monkeypatch):
    """Env var present before ``with`` is absent during, restored after."""
    from omnigent.harnesses.claude_sdk_executor import _unset_env_var

    monkeypatch.setenv("CLAUDECODE", "parent-value")
    with _unset_env_var("CLAUDECODE"):
        assert "CLAUDECODE" not in os.environ
    assert os.environ["CLAUDECODE"] == "parent-value"


def test_unset_env_var_noop_when_unset(monkeypatch):
    """When env var is not set before ``with``, block runs cleanly and key stays unset."""
    from omnigent.harnesses.claude_sdk_executor import _unset_env_var

    monkeypatch.delenv("CLAUDECODE", raising=False)
    with _unset_env_var("CLAUDECODE"):
        assert "CLAUDECODE" not in os.environ
    assert "CLAUDECODE" not in os.environ


def test_unset_env_var_restores_on_exception(monkeypatch):
    """Restoration must still happen when the block raises."""
    from omnigent.harnesses.claude_sdk_executor import _unset_env_var

    monkeypatch.setenv("CLAUDECODE", "original")
    with pytest.raises(RuntimeError, match="boom"):
        with _unset_env_var("CLAUDECODE"):
            assert "CLAUDECODE" not in os.environ
            raise RuntimeError("boom")
    assert os.environ["CLAUDECODE"] == "original"


def test_non_databricks_model_without_routing_does_not_raise() -> None:
    """Non-``databricks-*`` model with ``gateway=False`` must not raise.

    Ensures the guard only fires on the ``databricks-`` prefix.
    """
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor(
        model="claude-3-5-sonnet-20241022",
        gateway=False,
    )
    assert not executor._gateway


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY stripping during connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_api_key_stripped_during_connect(monkeypatch):
    """``_get_or_create_client`` must strip ``ANTHROPIC_API_KEY`` from
    ``os.environ`` during the ``connect()`` window so the Claude CLI
    uses subscription auth instead of a developer API key.

    This test drives ``_get_or_create_client`` with a stub SDK and
    captures ``os.environ`` at the moment ``connect()`` is invoked,
    ensuring both ``CLAUDECODE`` and ``ANTHROPIC_API_KEY`` are absent.
    """
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    # Env snapshot captured inside connect() -- proves the real code
    # path strips the keys, not just a standalone _unset_env_var call.
    connect_env: dict[str, str] = {}

    class _StubClient:
        """Minimal client whose connect() captures os.environ."""

        def __init__(self, options):
            """Store options for protocol compliance.

            :param options: SDK options object (unused by stub).
            """
            self.options = options
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            """Record env state at the moment the real code calls connect."""
            connect_env.update(os.environ)

        async def disconnect(self) -> None:
            """No-op disconnect."""

    class _StubSDK:
        """Minimal SDK whose ClaudeSDKClient captures env during connect."""

        ClaudeSDKClient = _StubClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("CLAUDECODE", "parent-value")

    executor = ClaudeSDKExecutor()
    options = SimpleNamespace()
    await executor._get_or_create_client(
        _StubSDK,  # type: ignore[arg-type]
        session_key="test-session",
        options=options,
        model=None,
    )

    # Both keys must be absent at the moment connect() ran.
    assert "ANTHROPIC_API_KEY" not in connect_env, (
        "ANTHROPIC_API_KEY leaked into connect() -- subscription auth bypassed"
    )
    assert "CLAUDECODE" not in connect_env, (
        "CLAUDECODE leaked into connect() -- nested-session error risk"
    )
    # Both are restored after _get_or_create_client returns.
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-secret"
    assert os.environ["CLAUDECODE"] == "parent-value"


@pytest.mark.asyncio
async def test_get_or_create_client_surfaces_cli_stderr_on_connect_timeout(monkeypatch):
    """A connect timeout must include the CLI's stderr tail in the
    raised ``TimeoutError`` so CI logs surface what the subprocess was
    doing while it hung.
    """
    from omnigent.harnesses import claude_sdk_executor as cse
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    monkeypatch.setattr(cse, "_CONNECT_TIMEOUT_SECONDS", 0.2)

    class _StubClient:
        def __init__(self, options) -> None:
            self.options = options
            # ``_force_close_client`` reads these on the failure path.
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            assert callable(self.options.stderr), (
                "executor must install a stderr callback before connect"
            )
            self.options.stderr("Spawning claude CLI...\n")
            self.options.stderr("ERROR: hanging on auth refresh\n")
            await asyncio.sleep(5.0)

        async def disconnect(self) -> None:
            pass

    class _StubSDK:
        ClaudeSDKClient = _StubClient

    executor = ClaudeSDKExecutor()
    options = SimpleNamespace(stderr=None)

    with pytest.raises(TimeoutError) as excinfo:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]
            session_key="test-session",
            options=options,
            model=None,
        )

    msg = str(excinfo.value)
    # ``(no CLI stderr captured)`` here means the tee callback was never wired.
    assert "Spawning claude CLI" in msg, msg
    assert "hanging on auth refresh" in msg, msg
    assert "after 0s" in msg
    # ``_tee_stderr`` must be unwired in the finally so the buffer can be
    # GC'd and post-connect stderr flows directly to the original callback.
    assert options.stderr is None


def test_resolve_sandbox_cwd_roots_relative_at_runner_workspace(monkeypatch) -> None:
    """A relative ``os_env.cwd`` (notably the default ``"."``) resolves
    against ``OMNIGENT_RUNNER_WORKSPACE`` — not the daemon's process cwd
    — so the sandbox root matches the tmux terminal and never falls back
    to ``$HOME``. Absolute paths keep their root and are resolved by
    ``Path.resolve(strict=False)``."""
    from omnigent.harnesses.claude_sdk_executor import _resolve_sandbox_cwd

    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/home/bobby/code/agents")
    monkeypatch.chdir("/tmp")

    # ``_resolve_sandbox_cwd`` ends in ``Path.resolve(strict=False)``. On macOS,
    # these literal paths route through firmlinks (``/home`` -> the automounter,
    # ``/tmp`` -> ``/private/tmp``), so compare against the same resolution
    # instead of literal strings. On Linux both sides are identical.
    workspace = Path("/home/bobby/code/agents").resolve(strict=False)
    assert _resolve_sandbox_cwd(".") == workspace
    assert _resolve_sandbox_cwd(None) == workspace
    assert _resolve_sandbox_cwd("src") == (workspace / "src").resolve(strict=False)
    assert _resolve_sandbox_cwd("/etc/foo") == Path("/etc/foo").resolve(strict=False)

    # No workspace set → falls back to the process cwd (prior behavior).
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    assert _resolve_sandbox_cwd(".") == Path("/tmp").resolve(strict=False)


@pytest.mark.parametrize("env_value", ["1", "true", "yes"])
def test_prepare_claude_cli_path_bypasses_wrapper_when_env_set(
    monkeypatch, caplog, env_value: str
) -> None:
    """
    ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX`` (any truthy value) must skip
    ``create_exec_launcher`` and hand back the raw CLI path. Used as a
    diagnostic knob to isolate the sandbox as a cause of the silent
    claude-sdk connect hang on the nightly Linux runner.
    """
    from omnigent.harnesses.claude_sdk_executor import prepare_claude_cli_path
    from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", env_value)

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not be called when bypass is enabled")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.create_exec_launcher",
        _fail_if_called,
    )

    spec = OSEnvSpec(
        type="caller_process",
        cwd="/tmp/work",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=["."],
            write_paths=["."],
            allow_network=True,
        ),
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.harnesses.claude_sdk_executor"):
        prepared = prepare_claude_cli_path("/usr/bin/claude", spec)

    assert prepared.cli_path == "/usr/bin/claude"
    # Bypass keeps native tools off so the diagnostic isolates the
    # CLI-startup variable; matches the fallback shape used when the
    # sandbox can't be applied for other reasons (line 569-574).
    assert prepared.enable_native_tools is False
    # The bypass must be loud in CI: a missing warning means a future
    # diagnostic run could silently fall through without us noticing.
    assert any("Sandbox bypass active" in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_prepare_tight_cli_process_path_bypasses_wrapper_when_env_set(monkeypatch) -> None:
    """``prepare_tight_cli_process_path`` must also honor the bypass env."""
    from omnigent.harnesses.claude_sdk_executor import prepare_tight_cli_process_path

    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not be called when bypass is enabled")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.create_exec_launcher",
        _fail_if_called,
    )

    assert prepare_tight_cli_process_path("/usr/bin/claude") == "/usr/bin/claude"


def _wrap_probe_spec():
    from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec

    return OSEnvSpec(
        type="caller_process",
        cwd="/tmp/work",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=["."],
            write_paths=["."],
            allow_network=True,
        ),
    )


def _active_policy():
    from omnigent.harnesses.sandbox import SandboxPolicy

    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=[Path("/tmp/work")],
        write_roots=[Path("/tmp/work")],
        write_files=[],
        allow_network=True,
    )


def test_prepare_claude_cli_path_degrades_when_resolve_sandbox_fails(monkeypatch, caplog) -> None:
    """
    A ``resolve_sandbox`` failure (e.g. ``sandbox-exec`` missing on the
    host) must NOT crash the seat at connect time. The prepare degrades
    to the raw CLI with native tools disabled — the same confinement
    shape as the ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX`` bypass — and warns.
    """
    from omnigent.harnesses.claude_sdk_executor import prepare_claude_cli_path

    def _raise_resolve(*args, **kwargs):
        raise OSError("darwin_seatbelt requires sandbox-exec on PATH")

    monkeypatch.setattr("omnigent.harnesses.claude_sdk_executor.resolve_sandbox", _raise_resolve)

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not run when resolve failed")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.create_exec_launcher", _fail_if_called
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.harnesses.claude_sdk_executor"):
        prepared = prepare_claude_cli_path("/usr/bin/claude", _wrap_probe_spec())

    assert prepared.cli_path == "/usr/bin/claude"
    assert prepared.enable_native_tools is False
    assert any(
        "Cannot resolve the configured sandbox" in record.getMessage() for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_prepare_claude_cli_path_degrades_when_wrap_probe_fails(monkeypatch, caplog) -> None:
    """
    When the backend can resolve but the spawn-time wrap can't be built
    (un-grantable interpreter layout, profile-size cap, cwd-scan
    overflow), the OSError previously fired inside ``run_launcher`` and
    killed the CLI spawn with an opaque connect timeout. The prepare-
    time probe must catch it and degrade — unwrapped CLI, native tools
    OFF, loud warning — never raise.
    """
    from omnigent.harnesses.claude_sdk_executor import prepare_claude_cli_path

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.resolve_sandbox",
        lambda *a, **k: _active_policy(),
    )

    class _RefusingBackend:
        def wrap_launcher_argv(self, *args, **kwargs):
            raise OSError("would require widening the sandbox read view")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.get_backend",
        lambda name: _RefusingBackend(),
    )

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not run when the wrap probe failed")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.create_exec_launcher", _fail_if_called
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.harnesses.claude_sdk_executor"):
        prepared = prepare_claude_cli_path("/usr/bin/claude", _wrap_probe_spec())

    assert prepared.cli_path == "/usr/bin/claude"
    assert prepared.enable_native_tools is False
    assert any("cannot wrap the Claude CLI" in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_prepare_claude_cli_path_probe_passes_target_and_still_wraps(
    monkeypatch,
) -> None:
    """
    The happy path is unchanged by the probe: the wrap still happens,
    native tools stay ON, and the probe exercises the same lane the
    launcher will (``target=<real CLI>``), so a probe pass means the
    run-time wrap can build the same grants.
    """
    from omnigent.harnesses.claude_sdk_executor import prepare_claude_cli_path

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.resolve_sandbox",
        lambda *a, **k: _active_policy(),
    )

    seen: dict[str, object] = {}

    class _OkBackend:
        def wrap_launcher_argv(self, argv, policy, cwd, chdir=None, target=None):
            seen["target"] = target
            return ["bwrap", "--", *argv]

    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.get_backend",
        lambda name: _OkBackend(),
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_sdk_executor.create_exec_launcher",
        lambda path, sandbox: "/tmp/launcher",
    )

    prepared = prepare_claude_cli_path("/usr/bin/claude", _wrap_probe_spec())

    assert prepared.cli_path == "/tmp/launcher"
    assert prepared.enable_native_tools is True
    assert seen["target"] == "/usr/bin/claude", (
        "The probe must exercise the target lane run_launcher will use."
    )


# ---------------------------------------------------------------------------
# Tests: _to_anthropic_content_blocks
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_to_anthropic_content_blocks_pdf_uses_base64_source() -> None:
    """PDF input_file blocks must use ``source.type = "base64"`` — the
    only MIME Anthropic accepts for the base64 document source shape."""
    pdf_data = _b64(b"%PDF-1.4 fake pdf content".decode("latin-1"))
    blocks = [{"type": "input_file", "file_data": f"data:application/pdf;base64,{pdf_data}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "base64"
    assert doc["source"]["media_type"] == "application/pdf"
    assert doc["source"]["data"] == pdf_data


def test_to_anthropic_content_blocks_markdown_uses_text_source() -> None:
    """Markdown input_file blocks must use ``source.type = "text"`` —
    Anthropic rejects ``base64`` source with non-PDF media types."""
    md_content = "# Hello\n\nThis is markdown."
    md_b64 = _b64(md_content)
    blocks = [{"type": "input_file", "file_data": f"data:text/markdown;base64,{md_b64}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "text"
    assert doc["source"]["media_type"] == "text/plain"
    assert doc["source"]["data"] == md_content


def test_to_anthropic_content_blocks_plain_text_uses_text_source() -> None:
    """``text/plain`` input_file blocks must also use ``source.type = "text"``."""
    content = "just some plain text"
    b64 = _b64(content)
    blocks = [{"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "text"
    assert doc["source"]["media_type"] == "text/plain"
    assert doc["source"]["data"] == content


# ---------------------------------------------------------------------------
# Tests: connect-failure stderr surfacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_client_surfaces_cli_stderr_on_connect_error() -> None:
    """A non-timeout connect failure includes captured CLI stderr."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _StubClient:
        def __init__(self, options: object) -> None:
            self._stderr_cb = getattr(options, "stderr", None)
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            if self._stderr_cb is not None:
                self._stderr_cb("unknown option '--no-session-persistence'\n")
            raise RuntimeError("Command failed with exit code 1; Check stderr output for details")

        async def disconnect(self) -> None:
            pass

    class _StubSDK:
        ClaudeSDKClient = _StubClient

    executor = ClaudeSDKExecutor()
    options = type("Opts", (), {"stderr": None})()

    with pytest.raises(RuntimeError) as excinfo:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]
            session_key="test-session",
            options=options,
            model=None,
        )

    msg = str(excinfo.value)
    assert "unknown option" in msg, (
        f"Stderr not included in connect error.\nExpected 'unknown option' in: {msg!r}"
    )
    assert "Command failed" in msg, msg


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Tests: TurnComplete.usage populated from ResultMessage.usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_message_usage_populates_turn_complete_usage() -> None:
    """``ResultMessage.usage`` flows through to ``TurnComplete.usage``.

    The claude-sdk executor must extract the provider-reported token counts
    from ``ResultMessage.usage`` and forward them via ``TurnComplete.usage``
    so the REPL context ring and ``/context`` command can display accurate
    per-turn fill without falling back to a local ``count_tokens()`` estimate.

    Regression guard: before this fix the executor yielded
    ``TurnComplete(response=..., usage=None)`` unconditionally, causing the
    context ring to stop updating after the first turn (the idle-event fallback
    only fired when ``host.tokens_used is None``, which was only true on the
    first turn once the local estimate had been set).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="Hello",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        usage={
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
        },
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))

    # Provider-reported counts must appear verbatim — if this is None the
    # REPL context ring will never update after the first turn for claude-sdk.
    assert turn.usage is not None, (
        "TurnComplete.usage is None — ResultMessage.usage was not captured. "
        "The context ring and /context command will show stale data after "
        "the first turn."
    )

    # input_tokens / output_tokens must come through from ResultMessage.usage
    assert turn.usage["input_tokens"] == 500, (
        f"input_tokens {turn.usage['input_tokens']} != 500 — "
        "ResultMessage.usage['input_tokens'] was not forwarded."
    )
    assert turn.usage["output_tokens"] == 200, (
        f"output_tokens {turn.usage['output_tokens']} != 200 — "
        "ResultMessage.usage['output_tokens'] was not forwarded."
    )

    # total_tokens is computed as input + output so the toolbar ring can
    # use usage["total_tokens"] without also needing individual fields.
    assert turn.usage["total_tokens"] == 700, (
        f"total_tokens {turn.usage['total_tokens']} != 700 (500+200) — "
        "total_tokens must be computed as input_tokens + output_tokens."
    )

    # Cache fields pass through as-is so the server can persist them.
    assert turn.usage.get("cache_read_input_tokens") == 50, (
        f"cache_read_input_tokens {turn.usage.get('cache_read_input_tokens')} != 50 — "
        "extra usage keys from ResultMessage.usage should be forwarded verbatim."
    )

    # context_tokens is the real prompt size = input + cache_creation +
    # cache_read (500 + 0 + 50). The runner reads this to track context-window
    # fill; without it the runner falls back to total_tokens (input + output,
    # non-cached only), which under-counts because the bulk of a long
    # conversation's prompt lives in cache_read_input_tokens. Note it differs
    # from total_tokens here (550 != 700) precisely because total ignores the
    # cached prompt while context includes it.
    assert turn.usage["context_tokens"] == 550, (
        f"context_tokens {turn.usage.get('context_tokens')} != 550 (500+0+50) — "
        "context_tokens must sum input + cache_creation + cache_read so the "
        "runner reports accurate, accumulating context-window fill."
    )

    # The resolved model is forwarded in usage so the server cost path can
    # price the turn even when the agent spec pins no llm.model. The key must
    # be present (here it's None — no model is configured in this unit test).
    assert "model" in turn.usage, (
        "TurnComplete.usage is missing the 'model' key — the server cost path "
        "relies on it to price relay turns whose spec pins no llm.model."
    )


@pytest.mark.asyncio
async def test_result_message_is_error_yields_executor_error() -> None:
    """``ResultMessage`` with ``is_error=True`` must surface as ``ExecutorError``.

    When the SDK signals a harness-level failure (e.g. expired login, not logged
    in), ``is_error`` is ``True`` and ``result`` carries the failure text.  The
    executor must route this into an ``ExecutorError`` — not store it as the
    assistant's response.

    Regression guard: before the fix, ``result`` was assigned to ``response_text``
    unconditionally, so the failure appeared as an assistant message with no error
    item and no log line.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import ExecutorError, TurnComplete

    class _Sentinel:
        pass

    sdk_result = SDKResultMessage(
        subtype="success",  # subtype is unreliable — is_error is the real signal
        session_id="s1",
        result="Not logged in · Please run /login",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=True,
        num_turns=1,
        usage=None,
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors, (
        "Expected at least one ExecutorError for is_error=True ResultMessage, got none. "
        "The failure text must not be stored as assistant content."
    )
    assert "Not logged in" in errors[0].message, (
        f"ExecutorError.message {errors[0].message!r} does not contain the failure text."
    )

    # Must not also appear as assistant content
    turns = [e for e in events if isinstance(e, TurnComplete)]
    for t in turns:
        assert "Not logged in" not in (t.response or ""), (
            "Failure text must not appear in TurnComplete.response"
            " — it leaked as assistant content."
        )


@pytest.mark.asyncio
async def test_provider_failure_assistant_message_becomes_coded_executor_error() -> None:
    """A CLI provider failure is a coded ``ExecutorError``, never assistant text.

    The CLI reports "Credit balance is too low" as an assistant message carrying
    ``error="billing_error"``, followed by a failing ``ResultMessage``.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import AssistantMessage as SDKAssistantMessage
    from claude_agent_sdk.types import ClaudeAgentOptions as SDKClaudeAgentOptions
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent
    from claude_agent_sdk.types import TextBlock as SDKTextBlock

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import ExecutorError, TextChunk, TurnComplete

    text = "Credit balance is too low"
    messages = [
        SDKAssistantMessage(
            content=[SDKTextBlock(text=text)], model="claude", error="billing_error"
        ),
        SDKResultMessage(
            subtype="success",
            session_id="s1",
            result=text,
            total_cost_usd=0.0,
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            usage=None,
        ),
    ]

    class _FakeSDK:
        AssistantMessage = SDKAssistantMessage
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        TextBlock = SDKTextBlock
        ClaudeAgentOptions = SDKClaudeAgentOptions

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert [e.code for e in errors] == ["insufficient_credit"]
    assert not [e for e in events if isinstance(e, TextChunk)]
    assert not [e for e in events if isinstance(e, TurnComplete)]


@pytest.mark.asyncio
async def test_context_tokens_uses_last_call_not_cumulative_on_multi_iteration_turn() -> None:
    """``context_tokens`` must reflect the LAST API call, not the cumulative sum.

    A single user turn can drive several internal API calls (the SDK's
    tool-use loop). ``ResultMessage.usage`` reports the CUMULATIVE usage
    across all of them, so its ``input_tokens`` / cache buckets are the
    sum of every iteration's prompt. Since each iteration re-sends the
    whole (growing) conversation, summing the prompt side K-fold
    over-counts context — which surfaced as the toolbar context ring
    spiking up during a tool-using turn and snapping back down on the
    next plain turn.

    Context-window FILL is a single prompt's size, not a billing sum: the
    final call's prompt already contains the full conversation that carries
    into the next turn. The executor captures each ``message_start``'s
    per-call usage and uses the LAST one for ``context_tokens``, while still
    reporting the cumulative totals for billing.

    Regression guard: before this fix ``context_tokens`` was
    ``cumulative_input + cumulative_cache_read`` (= 21100 here), which is
    the K-fold over-count. The correct value is the last call's prompt
    (= 10800).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    def _message_start(usage: dict[str, int]) -> SDKStreamEvent:
        # One ``message_start`` opens one API call and carries that call's
        # prompt-side usage. Two of them model a two-call (one tool-loop)
        # turn; the executor must keep only the last for context.
        return SDKStreamEvent(
            uuid="u",
            session_id="s1",
            event={"type": "message_start", "message": {"usage": usage}},
        )

    # Call 1 (cold-ish): non-cached input 300 + 10000 read from cache.
    first_call = _message_start(
        {"input_tokens": 300, "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 0}
    )
    # Call 2 (final, after the tool result is appended): the conversation
    # grew, so this prompt is the real context = 500 + 10300 = 10800.
    second_call = _message_start(
        {"input_tokens": 500, "cache_read_input_tokens": 10300, "cache_creation_input_tokens": 0}
    )
    # ResultMessage carries the CUMULATIVE sum across both calls:
    # input 800, cache_read 20300, output 250.
    cumulative_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="done",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=2,
        usage={
            "input_tokens": 800,
            "output_tokens": 250,
            "cache_read_input_tokens": 20300,
            "cache_creation_input_tokens": 0,
        },
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [first_call, second_call, cumulative_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None, "TurnComplete.usage is None on a multi-call turn."

    # context_tokens = LAST call's prompt (500 + 0 + 10300), NOT the
    # cumulative sum (800 + 20300 = 21100). This is the whole point of the
    # fix: a tool-using turn must not inflate the context ring.
    assert turn.usage["context_tokens"] == 10800, (
        f"context_tokens {turn.usage.get('context_tokens')} != 10800 — must be the "
        "LAST message_start call's prompt (input+cache_creation+cache_read), not the "
        "cumulative ResultMessage sum (21100). Using the cumulative value over-counts "
        "context K-fold on a K-call turn."
    )
    assert turn.usage["context_tokens"] != 21100, (
        "context_tokens equals the cumulative over-count (21100) — the executor is "
        "summing prompt usage across tool-loop iterations instead of taking the last call."
    )

    # Billing totals still come from the cumulative ResultMessage.usage so
    # cost is charged for every call, not just the last one.
    assert turn.usage["input_tokens"] == 800, (
        f"input_tokens {turn.usage.get('input_tokens')} != 800 — billing input must "
        "stay the cumulative cross-call sum from ResultMessage.usage."
    )
    assert turn.usage["output_tokens"] == 250, (
        f"output_tokens {turn.usage.get('output_tokens')} != 250 — billing output must "
        "stay the cumulative sum from ResultMessage.usage."
    )
    assert turn.usage["total_tokens"] == 1050, (
        f"total_tokens {turn.usage.get('total_tokens')} != 1050 (800+250) — total must "
        "be the cumulative input + output for billing."
    )


@pytest.mark.asyncio
async def test_context_tokens_emitted_when_turn_ends_without_result_message() -> None:
    """A turn that never reaches ``ResultMessage`` still reports ``context_tokens``.

    ``context_tokens`` (context-window fill) is normally assembled in the
    ``ResultMessage`` branch at successful completion. But a turn can end the
    stream without a ``ResultMessage`` — the CLI can close the stream early,
    or the turn can be cut short before its final usage is reported. Before
    this fix the executor yielded ``TurnComplete(usage=None)`` in that case,
    so the occupancy meter froze at the previous successful turn's value and
    showed a misleadingly low fill exactly when a session was in trouble
    (#1533).

    The per-call prompt size is already observed mid-turn from each
    ``message_start`` event, so when no ``ResultMessage`` arrives the executor
    must fall back to that observed usage and still emit ``context_tokens``.

    Regression guard: pre-fix ``turn.usage`` is ``None`` here because the
    stream carries only a ``message_start`` and then stops.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    # The turn opens one API call (carrying its prompt usage) and then the
    # stream simply ends — no ResultMessage, mirroring an early CLI stream
    # close or a turn cut short before final usage is reported.
    message_start = SDKStreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 400,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 100,
                }
            },
        },
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [message_start]  # note: no ResultMessage

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None, (
        "TurnComplete.usage is None on a turn that ended without a ResultMessage — "
        "the observed message_start prompt usage was discarded, so the occupancy "
        "meter freezes at the last successful turn's value (#1533)."
    )

    # context_tokens = the observed prompt = input + cache_creation + cache_read
    # (400 + 100 + 9000), so a failed/early-ending turn still refreshes fill.
    assert turn.usage["context_tokens"] == 9500, (
        f"context_tokens {turn.usage.get('context_tokens')} != 9500 (400+100+9000) — "
        "an incomplete turn must report context_tokens from the last observed "
        "message_start prompt so the occupancy meter does not freeze."
    )
    # output_tokens is unknown on an incomplete turn, so it is reported as 0
    # rather than guessed; the meaningful field here is context_tokens.
    assert turn.usage["output_tokens"] == 0, (
        f"output_tokens {turn.usage.get('output_tokens')} != 0 — output is unknown "
        "on an incomplete turn and must not be fabricated."
    )
    assert "model" in turn.usage, (
        "TurnComplete.usage is missing the 'model' key on the incomplete-turn path."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic_error", [False, True])
@pytest.mark.parametrize("observed", [True, False])
async def test_assistant_message_model_flows_to_turn_usage(
    synthetic_error: bool,
    observed: bool,
) -> None:
    """The SDK's assistant-message model is forwarded in ``TurnComplete.usage``.

    When the agent spec pins no model (a delegating supervisor on the gateway),
    the resolved config model is ``None`` — but the Claude SDK reports the
    concrete model on each ``AssistantMessage``. The executor captures it and
    forwards it as ``usage["model"]`` so the server cost path can price the
    turn. A regression here leaves cost unpriced ("—") for every unpinned
    claude-sdk agent (the debbie/debby supervisors).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _AsstMsg:
        """Minimal stand-in for the SDK AssistantMessage (carries model)."""

        def __init__(self, content: list[object], model: str) -> None:
            self.content = content
            self.model = model

    assistant = _AsstMsg(content=[], model="claude-opus-4-8")
    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="hi",
        total_cost_usd=0.0,
        duration_ms=1,
        duration_api_ms=1,
        is_error=synthetic_error,
        num_turns=1,
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    class _FakeSDK:
        AssistantMessage = _AsstMsg
        UserMessage = type("_U", (), {})
        SystemMessage = type("_S", (), {})
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [assistant] if observed else []
        if synthetic_error:
            messages.append(_AsstMsg(content=[], model="<synthetic>"))
        messages.append(sdk_result)

        class ClaudeSDKClient:
            def __init__(self, options: object) -> None:
                self.options = options

            async def connect(self) -> None:
                return None

            async def query(self, prompt: object, session_id: str = "default") -> None:
                return None

            async def receive_response(self):  # type: ignore[no-untyped-def]
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self) -> None:
                return None

    executor = ClaudeSDKExecutor(model=None if observed else "configured-model")
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    terminal_type = ExecutorError if synthetic_error else TurnComplete
    turn = next(e for e in events if isinstance(e, terminal_type))
    assert turn.usage is not None
    # Synthetic errors preserve the last real report, or the configured fallback.
    expected_model = "claude-opus-4-8" if observed else "configured-model"
    assert turn.usage["model"] == expected_model


@pytest.mark.asyncio
async def test_result_message_usage_none_yields_turn_complete_without_usage() -> None:
    """When ``ResultMessage.usage`` is ``None``, ``TurnComplete.usage`` is ``None``.

    The executor must not synthesize fake usage when the SDK doesn't report it
    (e.g. Databricks gateway not returning usage). ``None`` usage causes the
    REPL to fall back to its local ``count_tokens()`` estimate, which is correct
    behaviour — no invented numbers should appear in the context ring.

    Regression guard: if the usage-capture code unconditionally sets
    ``turn_usage = {}`` (or any non-None dict), the REPL would display
    ``0 / 200k (0%)`` instead of triggering the local estimate.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="Hello",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        usage=None,  # SDK didn't report usage
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))

    # usage=None triggers the REPL's local count_tokens() fallback,
    # which is the right behaviour when the SDK doesn't report usage.
    # A non-None empty dict {} would short-circuit the fallback with 0 tokens.
    assert turn.usage is None, (
        f"TurnComplete.usage should be None when ResultMessage.usage is None, "
        f"got {turn.usage!r}. An empty dict would display 0 tokens in the ring."
    )


# ---------------------------------------------------------------------------
# Tests: pre-execution TOOL_CALL policy gate (can_use_tool)
# ---------------------------------------------------------------------------


class TestToolCallPolicyGate(unittest.TestCase):
    """The ``can_use_tool`` gate that enforces TOOL_CALL policy on
    connector-native MCP tools (``mcp__github__*`` etc.) before they run.
    """

    def _make_executor(self, permission_mode="bypassPermissions"):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        return ClaudeSDKExecutor(permission_mode=permission_mode)

    @staticmethod
    def _verdict(action, reason=None):
        from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

        return PolicyVerdictPayload(action=action, reason=reason)

    @staticmethod
    def _perm_ctx():
        return SimpleNamespace(tool_use_id="tu_1", agent_id=None, suggestions=[])

    def test_connector_native_tool_triggers_tool_call_eval(self):
        """A connector-native tool name drives a PHASE_TOOL_CALL evaluation
        with the SDK-native tool name and arguments."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor()
            evaluator = AsyncMock(return_value=self._verdict("POLICY_ACTION_ALLOW"))
            executor._policy_evaluator = evaluator

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug", "body": "x"},
                self._perm_ctx(),
            )

            evaluator.assert_awaited_once()
            phase, data = evaluator.await_args.args
            self.assertEqual(phase, "PHASE_TOOL_CALL")
            self.assertEqual(
                data,
                {
                    "name": "mcp__github__issue_write",
                    "arguments": {"title": "bug", "body": "x"},
                },
            )
            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_deny_verdict_blocks_execution(self):
        """A DENY verdict returns PermissionResultDeny carrying the
        policy's reason and never reaches the elicitation handler."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="default")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_DENY", reason="no writes to github")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "no writes to github")
            # DENY short-circuits — no human prompt.
            elicit.assert_not_awaited()

        _run(_t())

    def test_ask_verdict_prompts_even_under_bypass(self):
        """A raw ASK verdict is supported by routing to Omnigent
        elicitation, even under bypassPermissions."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            elicit.assert_awaited_once_with("mcp__github__issue_write", {"title": "bug"})

        _run(_t())

    def test_ask_verdict_denies_when_user_declines(self):
        """A declined raw ASK blocks execution with the policy reason."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )
            executor._elicitation_handler = AsyncMock(return_value=False)

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "approval required")

        _run(_t())

    def test_ask_verdict_without_elicitation_handler_fails_closed(self):
        """If raw ASK reaches the callback but no handler is available,
        the tool must not run."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "approval required")

        _run(_t())

    def test_unspecified_verdict_falls_through(self):
        """UNSPECIFIED is a proto no-op verdict and should behave like no match."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_UNSPECIFIED")
            )

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_read",
                {"number": 1},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_unexpected_verdict_fails_closed(self):
        """Unknown policy actions should not silently allow a tool call."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(return_value=self._verdict("BOGUS"))

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertIn("Unexpected Omnigent TOOL_CALL policy verdict", result.message)

        _run(_t())

    def test_allow_verdict_no_human_prompt_under_bypass(self):
        """ALLOW under bypassPermissions allows the call with no human
        prompt, preserving bypass ergonomics."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_read",
                {"number": 1},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            elicit.assert_not_awaited()

        _run(_t())

    def test_no_policy_evaluator_no_match_allows_silently(self):
        """With no policy evaluator wired (default ALLOW), the gate allows
        with no evaluation and no human prompt under bypassPermissions."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            # No _policy_evaluator set, no elicitation handler.
            result = await executor._can_use_tool_gate(
                "mcp__atlassian__createJiraIssue",
                {"summary": "x"},
                self._perm_ctx(),
            )
            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_omnigent_own_tool_skips_evaluation(self):
        """``mcp__omnigent__*`` tools are already TOOL_CALL-gated server-side
        via the dispatch bridge / ProxyMcpManager, so the gate must NOT
        evaluate them again (avoids double-evaluation)."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            evaluator = AsyncMock(return_value=self._verdict("POLICY_ACTION_ALLOW"))
            executor._policy_evaluator = evaluator

            result = await executor._can_use_tool_gate(
                "mcp__omnigent__sys_os_read",
                {"path": "/tmp/x"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            evaluator.assert_not_awaited()

        _run(_t())

    def test_non_bypass_runs_elicitation_after_policy_allow(self):
        """In a non-bypass mode, a policy ALLOW falls through to the
        human-consent elicitation gate (pre-existing behavior)."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="default")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )

            # Human approves.
            executor._elicitation_handler = AsyncMock(return_value=True)
            approved = await executor._can_use_tool_gate(
                "mcp__github__issue_read", {}, self._perm_ctx()
            )
            self.assertIsInstance(approved, PermissionResultAllow)

            # Human denies.
            executor._elicitation_handler = AsyncMock(return_value=False)
            denied = await executor._can_use_tool_gate(
                "mcp__github__issue_read", {}, self._perm_ctx()
            )
            self.assertIsInstance(denied, PermissionResultDeny)

        _run(_t())

    def test_gate_installed_under_bypass_permissions(self):
        """run_turn installs the can_use_tool gate even under
        bypassPermissions when a policy evaluator is wired — the
        regression this feature fixes."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )

            captured = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["can_use_tool"] = options.can_use_tool
                raise RuntimeError("stop after options build")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            # Bound-method identity differs per access; compare equality.
            self.assertEqual(captured["can_use_tool"], executor._can_use_tool_gate)
            self.assertIsNotNone(captured["can_use_tool"])

        _run(_t())

    def test_gate_not_installed_without_evaluator_or_handler(self):
        """With neither a policy evaluator nor an elicitation handler, no
        can_use_tool callback is installed (unchanged baseline)."""
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(permission_mode="bypassPermissions")
            captured = {}

            async def fake_get_or_create_client(
                sdk, *, session_key, options, model, rebuild_on_instructions_change=False, **_kw
            ):
                captured["can_use_tool"] = options.can_use_tool
                raise RuntimeError("stop after options build")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["can_use_tool"])

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: Compaction detection via PreCompact hook
# ---------------------------------------------------------------------------


def test_precompact_hook_emits_compaction_complete_with_session_messages() -> None:
    """When PreCompact fires and a ResultMessage carries a session_id,
    CompactionComplete is emitted with compacted_messages read from
    the CLI's session transcript."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete

    class _ResultMessage:
        def __init__(self, session_id, result):
            self.session_id = session_id
            self.result = result
            self.content = []
            self.model = "claude-test"
            self.usage = type(
                "U",
                (),
                {
                    "input_tokens": 500,
                    "output_tokens": 100,
                },
            )()

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name

    class _HookEventMessage(_SystemMessage):
        pass

    class _FakeSessionMessage:
        def __init__(self, type, message):
            self.type = type
            self.message = message

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )
        messages: list = []

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                _FakeSDK.messages = [
                    _HookEventMessage(
                        subtype="hook_started",
                        data={"hook_event": "PreCompact"},
                        hook_event_name="PreCompact",
                    ),
                    _ResultMessage("claude-uuid-123", "compacted result"),
                ]

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    fake_session_msgs = [
        _FakeSessionMessage("user", {"content": [{"type": "text", "text": "hi"}]}),
        _FakeSessionMessage("assistant", {"content": [{"type": "text", "text": "compacted"}]}),
    ]

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch(
                "claude_agent_sdk.get_session_messages",
                return_value=fake_session_msgs,
            ) as mock_get_msgs,
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 1
        ce = compaction_events[0]
        assert ce.compacted_messages is not None
        assert len(ce.compacted_messages) == 2
        assert ce.compacted_messages[0]["role"] == "user"
        assert ce.compacted_messages[1]["role"] == "assistant"
        mock_get_msgs.assert_called_once_with("claude-uuid-123", directory=None)
        # CompactionStarted before CompactionComplete before TurnComplete
        from omnigent.harnesses.executor import CompactionStarted

        started_events = [e for e in events if isinstance(e, CompactionStarted)]
        assert len(started_events) == 1
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) == 1
        assert events.index(started_events[0]) < events.index(compaction_events[0])
        assert events.index(compaction_events[0]) < events.index(turn_completes[0])

    _run(_t())


def test_precompact_exception_persists_compaction_before_executor_error(tmp_path: Path) -> None:
    """A stream failure after PreCompact still emits the durable boundary."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete, CompactionStarted

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None, session_id=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name
            self.session_id = session_id

    class _FakeSessionMessage:
        def __init__(self, type, message):
            self.type = type
            self.message = message

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = type("ResultMessage", (), {})
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                return

            async def receive_response(self):
                yield _SystemMessage(
                    subtype="hook_started",
                    data={
                        "hook_event": "PreCompact",
                        "session_id": "claude-uuid-error",
                        "transcript_path": str(transcript_path),
                    },
                    hook_event_name="PreCompact",
                    session_id="claude-uuid-error",
                )
                with transcript_path.open("a", encoding="utf-8") as transcript:
                    transcript.write(
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "compact-summary-1",
                                "isCompactSummary": True,
                                "message": {
                                    "role": "user",
                                    "content": "compacted summary",
                                },
                            }
                        )
                        + "\n"
                    )
                raise RuntimeError("Autocompact is thrashing: the context refilled to the limit")

            async def disconnect(self):
                return None

    fake_session_msgs = [
        _FakeSessionMessage(
            "assistant",
            {"content": [{"type": "text", "text": "compacted summary"}]},
        )
    ]
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "precompact-1",
                "message": {"role": "user", "content": "continue"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch(
                "claude_agent_sdk.get_session_messages",
                return_value=fake_session_msgs,
            ) as mock_get_msgs,
        ):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "continue", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        started = [event for event in events if isinstance(event, CompactionStarted)]
        completed = [event for event in events if isinstance(event, CompactionComplete)]
        errors = [event for event in events if isinstance(event, ExecutorError)]

        assert len(started) == 1
        assert len(completed) == 1
        assert len(errors) == 1
        assert not [event for event in events if isinstance(event, TurnComplete)]
        assert events.index(started[0]) < events.index(completed[0])
        assert events.index(completed[0]) < events.index(errors[0])
        assert completed[0].compacted_messages == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "compacted summary"}],
            }
        ]
        mock_get_msgs.assert_called_once_with("claude-uuid-error", directory=None)

    _run(_t())


@pytest.mark.parametrize(
    ("transcript_location", "expect_checkpoint"),
    [("top_level", True), ("nested", False)],
)
def test_precompact_exception_sdk_wire_payload_contract(
    tmp_path: Path,
    transcript_location: str,
    expect_checkpoint: bool,
) -> None:
    """The SDK-decoded hook payload exposes a safe transcript boundary."""
    import claude_agent_sdk as real_sdk
    from claude_agent_sdk._internal.message_parser import parse_message

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete

    class _FakeSessionMessage:
        def __init__(self, type, message):
            self.type = type
            self.message = message

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "precompact-wire-1",
                "message": {"role": "user", "content": "continue"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    raw_hook_payload = {
        "type": "system",
        "subtype": "hook_started",
        "hook_id": "hook-precompact-wire-1",
        "hook_name": "PreCompact:auto",
        "hook_event": "PreCompact",
        "uuid": "hook-event-precompact-wire-1",
        "session_id": "claude-uuid-wire",
        "cwd": str(tmp_path),
        "trigger": "auto",
        "custom_instructions": None,
    }
    if transcript_location == "top_level":
        raw_hook_payload["transcript_path"] = str(transcript_path)
    else:
        raw_hook_payload["input"] = {"transcript_path": str(transcript_path)}

    hook_message = parse_message(raw_hook_payload)
    assert isinstance(hook_message, real_sdk.HookEventMessage)

    class _FakeSDK:
        AssistantMessage = real_sdk.AssistantMessage
        UserMessage = real_sdk.UserMessage
        SystemMessage = real_sdk.SystemMessage
        ResultMessage = real_sdk.ResultMessage
        StreamEvent = real_sdk.StreamEvent
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                return

            async def receive_response(self):
                yield hook_message
                with transcript_path.open("a", encoding="utf-8") as transcript:
                    transcript.write(
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "compact-summary-wire-1",
                                "isCompactSummary": True,
                                "message": {
                                    "role": "user",
                                    "content": "compacted summary",
                                },
                            }
                        )
                        + "\n"
                    )
                raise RuntimeError("Autocompact refilled the context")

            async def disconnect(self):
                return None

    fake_session_msgs = [
        _FakeSessionMessage(
            "assistant",
            {"content": [{"type": "text", "text": "compacted summary"}]},
        )
    ]

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch(
                "claude_agent_sdk.get_session_messages",
                return_value=fake_session_msgs,
            ) as mock_get_messages,
        ):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "continue", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        checkpoints = [event for event in events if isinstance(event, CompactionComplete)]
        assert len(checkpoints) == int(expect_checkpoint)
        if expect_checkpoint:
            mock_get_messages.assert_called_once_with("claude-uuid-wire", directory=None)
        else:
            mock_get_messages.assert_not_called()

    _run(_t())


@pytest.mark.parametrize("session_read", [[], RuntimeError("transcript not flushed")])
def test_precompact_exception_preserves_history_without_exportable_checkpoint(
    tmp_path: Path,
    session_read: list[object] | Exception,
) -> None:
    """An empty or failed export never replaces full history with a placeholder."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None, session_id=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name
            self.session_id = session_id

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = type("ResultMessage", (), {})
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                return

            async def receive_response(self):
                yield _SystemMessage(
                    subtype="hook_started",
                    data={
                        "hook_event": "PreCompact",
                        "session_id": "claude-uuid-error",
                        "transcript_path": str(transcript_path),
                    },
                    hook_event_name="PreCompact",
                    session_id="claude-uuid-error",
                )
                with transcript_path.open("a", encoding="utf-8") as transcript:
                    transcript.write(
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "compact-summary-1",
                                "isCompactSummary": True,
                                "message": {
                                    "role": "user",
                                    "content": "compacted summary",
                                },
                            }
                        )
                        + "\n"
                    )
                raise RuntimeError("Autocompact is thrashing: the context refilled to the limit")

            async def disconnect(self):
                return None

    async def _t():
        executor = ClaudeSDKExecutor()
        get_messages = (
            patch("claude_agent_sdk.get_session_messages", side_effect=session_read)
            if isinstance(session_read, Exception)
            else patch("claude_agent_sdk.get_session_messages", return_value=session_read)
        )
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            get_messages,
        ):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "continue", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        assert not [event for event in events if isinstance(event, CompactionComplete)]
        assert len([event for event in events if isinstance(event, ExecutorError)]) == 1

    _run(_t())


def test_precompact_exception_without_new_summary_preserves_history(tmp_path: Path) -> None:
    """PreCompact alone is not evidence that Claude finished compaction."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None, session_id=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name
            self.session_id = session_id

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = type("ResultMessage", (), {})
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                return

            async def receive_response(self):
                yield _SystemMessage(
                    subtype="hook_started",
                    data={
                        "hook_event": "PreCompact",
                        "session_id": "claude-uuid-error",
                        "transcript_path": str(transcript_path),
                    },
                    hook_event_name="PreCompact",
                    session_id="claude-uuid-error",
                )
                raise RuntimeError("stream failed before compaction completed")

            async def disconnect(self):
                return None

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch("claude_agent_sdk.get_session_messages") as mock_get_messages,
        ):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "continue", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        assert not [event for event in events if isinstance(event, CompactionComplete)]
        assert len([event for event in events if isinstance(event, ExecutorError)]) == 1
        mock_get_messages.assert_not_called()

    _run(_t())


def test_precompact_hook_emits_compaction_started_before_complete() -> None:
    """CompactionStarted is yielded when PreCompact fires, before CompactionComplete.

    This ensures clients see the in-progress signal while compaction is
    still happening, rather than both events arriving back-to-back after
    the turn ends.
    """
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete, CompactionStarted

    class _ResultMessage:
        def __init__(self, session_id, result):
            self.session_id = session_id
            self.result = result
            self.content = []
            self.model = "claude-test"
            self.usage = type(
                "U",
                (),
                {"input_tokens": 500, "output_tokens": 100},
            )()

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name

    class _FakeSessionMessage:
        def __init__(self, type, message):
            self.type = type
            self.message = message

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )
        messages: list = []

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                _FakeSDK.messages = [
                    _SystemMessage(
                        subtype="hook_started",
                        data={"hook_event": "PreCompact"},
                        hook_event_name="PreCompact",
                    ),
                    _ResultMessage("claude-uuid-456", "compacted result"),
                ]

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch(
                "claude_agent_sdk.get_session_messages",
                return_value=[
                    _FakeSessionMessage(
                        "user",
                        {"content": [{"type": "text", "text": "compacted summary"}]},
                    )
                ],
            ),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        started = [e for e in events if isinstance(e, CompactionStarted)]
        completed = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(started) == 1, "expected exactly one CompactionStarted"
        assert len(completed) == 1, "expected exactly one CompactionComplete"
        assert events.index(started[0]) < events.index(completed[0]), (
            "CompactionStarted must precede CompactionComplete"
        )

    _run(_t())


def test_no_precompact_no_compaction_event() -> None:
    """When no PreCompact hook fires, no CompactionComplete is yielded."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import CompactionComplete

    class _ResultMessage:
        def __init__(self, session_id, result):
            self.session_id = session_id
            self.result = result
            self.content = []
            self.model = "claude-test"
            self.usage = type(
                "U",
                (),
                {
                    "input_tokens": 500,
                    "output_tokens": 100,
                },
            )()

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )
        messages: list = []

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                _FakeSDK.messages = [_ResultMessage(session_id, "normal result")]

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    async def _t():
        executor = ClaudeSDKExecutor()
        with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 0

    _run(_t())


def test_find_system_claude_delegates_to_shared_resolver(monkeypatch) -> None:
    """``_find_system_claude`` resolves claude via the shared resolver with the
    OMNIGENT_CLAUDE_PATH override, so an nvm/npm-installed claude off the host
    daemon's frozen PATH is still found. (The resolver's PATH/override/fallback
    behavior is covered in tests/harnesses/test_proc_and_platform.py.)"""
    from omnigent.harnesses import claude_sdk_executor as cse

    captured = {}

    def fake_resolve(name, *, env_var=None):
        captured["name"] = name
        captured["env_var"] = env_var
        return "/opt/homebrew/bin/claude"

    monkeypatch.setattr(cse, "resolve_cli_binary", fake_resolve)
    assert cse._find_system_claude() == "/opt/homebrew/bin/claude"
    assert captured == {"name": "claude", "env_var": "OMNIGENT_CLAUDE_PATH"}


def test_claude_sdk_claims_live_message_queue() -> None:
    """ClaudeSDKExecutor steers a running turn (see tests/harnesses/test_claude_sdk_steer.py).

    The one-turn-behind desync of issue #3472 came from writing to the CLI while nobody
    read the extra answer; the stream now reads on until every pushed message is answered.
    """
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor()
    assert executor.supports_live_message_queue() is True


@pytest.mark.asyncio
async def test_enqueue_session_message_returns_false_without_queuing() -> None:
    """With no turn running, enqueue_session_message returns False and never calls query().

    A write to a CLI whose answer nobody reads is the desync from issue #3472; only a
    running turn has a steer window (tests/harnesses/test_claude_sdk_steer.py).
    """
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor()
    query_called = False

    class _FakeClient:
        async def query(self, *args, **kwargs):
            nonlocal query_called
            query_called = True

    # Inject a fake client state so the session key is known.
    executor._clients["s1"] = SimpleNamespace(client=_FakeClient())

    result = await executor.enqueue_session_message("s1", "hello")

    assert result is False
    assert not query_called, "query() must not be called during enqueue"


@pytest.mark.asyncio
async def test_terminal_error_carries_observed_usage() -> None:
    """A terminal-error turn still reports the usage it observed mid-turn.

    A turn can fail AFTER the model call started — the stream dies and the
    CLI's retries are rejected (auth failure), or the ``ResultMessage``
    arrives with ``is_error=True``. The prompt size was already observed
    from ``message_start``, and discarding it froze the context-occupancy
    meter at the previous successful turn's value exactly when the session
    was in trouble.

    Regression guard: pre-fix the ``terminal_error`` path yielded
    ``ExecutorError`` with no usage, so nothing downstream could update
    the meter on a failed turn.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.executor import ExecutorError

    class _Sentinel:
        pass

    # The model call opens (message_start carries the prompt usage), then the
    # harness reports a terminal failure via ResultMessage(is_error=True).
    message_start = SDKStreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 400,
                    "cache_read_input_tokens": 99_000,
                    "cache_creation_input_tokens": 600,
                }
            },
        },
    )
    sdk_result = SDKResultMessage(
        subtype="success",
        session_id="s1",
        result="authentication failed",
        total_cost_usd=0.0,
        duration_ms=10,
        duration_api_ms=8,
        is_error=True,
        num_turns=1,
        usage=None,
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [message_start, sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.harnesses.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors, "Expected an ExecutorError for the terminal failure"
    usage = errors[0].usage
    assert usage is not None, (
        "ExecutorError.usage is None — the observed message_start usage was "
        "discarded on the terminal-error path, so the context meter freezes."
    )
    # Window fill = input + cache_creation + cache_read from the last call.
    assert usage["context_tokens"] == 100_000
    # output_tokens is unknown on an incomplete turn — reported as 0.
    assert usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Tests: a rebuilt client is a cold start
# ---------------------------------------------------------------------------


class TestRebuiltClientReplaysHistory(unittest.TestCase):
    """The prompt follows the client that receives it, not the one that was warm."""

    _HISTORY = [
        {"role": "user", "content": "first question", "session_id": "s1"},
        {"role": "assistant", "content": "first answer", "session_id": "s1"},
        {"role": "user", "content": "second question", "session_id": "s1"},
    ]

    def _sent_prompt(self, *, rebuild: bool) -> object:
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._force_close_client = AsyncMock()  # type: ignore[method-assign]

            def _state():
                client = SimpleNamespace(disconnect=AsyncMock())
                return SimpleNamespace(loop=None, task=None, client=client)

            executor._clients["s1"] = _state()  # type: ignore[assignment]
            sent: list[object] = []

            class _Client:
                async def query(self, prompt, session_id=None):
                    sent.append(prompt)
                    raise RuntimeError("stop after the prompt is sent")

            async def fake_get_or_create_client(sdk, **kwargs):
                if rebuild:
                    executor._clients["s1"] = _state()  # type: ignore[assignment]
                return _Client()

            with patch.object(
                executor, "_get_or_create_client", side_effect=fake_get_or_create_client
            ):
                async for _ in executor.run_turn(list(self._HISTORY), [], ""):
                    pass
            return sent[0]

        return _run(_t())

    def test_rebuilt_client_gets_full_history(self):
        prompt = self._sent_prompt(rebuild=True)
        self.assertIn("Conversation so far:", prompt)
        self.assertIn("first answer", prompt)
        self.assertIn("second question", prompt)

    def test_warm_client_still_gets_only_trailing_user_content(self):
        self.assertEqual(self._sent_prompt(rebuild=False), "second question")


class TestPrelaunch(unittest.TestCase):
    """ADR 0026: the runner launches the Claude CLI at session init with an empty first message,
    so the first real turn finds it warm and still replays the whole history into it."""

    @staticmethod
    def _fake_sdk(clients: list, prompts: list):
        base = TestSupersideChatEngineOptions._fake_sdk(clients)

        class _SDK(base):  # type: ignore[misc, valid-type]
            class ClaudeSDKClient(base.ClaudeSDKClient):  # type: ignore[misc, name-defined]
                async def query(self, prompt, session_id="default"):
                    prompts.append(prompt)

        return _SDK

    def _run(self, turns):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        clients: list = []
        prompts: list = []

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=self._fake_sdk(clients, prompts),
            ):
                executor = ClaudeSDKExecutor()
                for messages, system_prompt, *config in turns:
                    events = [
                        e async for e in executor.run_turn(messages, [], system_prompt, *config)
                    ]
                    self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())
        return clients, prompts

    _EMPTY = [{"role": "user", "content": [], "session_id": "s"}]
    _HISTORY = [
        {"role": "user", "content": "first question", "session_id": "s"},
        {"role": "assistant", "content": "first answer", "session_id": "s"},
        {"role": "user", "content": "make the deck", "session_id": "s"},
    ]

    def test_prelaunched_cli_serves_the_first_turn_with_the_full_history(self):
        clients, prompts = self._run([(self._EMPTY, "prompt"), (self._HISTORY, "prompt")])
        self.assertEqual(len(clients), 1, "the first turn reuses the pre-launched CLI")
        self.assertEqual(len(prompts), 1, "the pre-launch sends the model nothing")
        self.assertIn("first question", str(prompts[0]))
        self.assertIn("make the deck", str(prompts[0]))

    def test_prelaunched_cli_launched_with_other_options_is_relaunched(self):
        clients, prompts = self._run([(self._EMPTY, "prompt"), (self._HISTORY, "new prompt")])
        self.assertEqual(len(clients), 2)
        self.assertIn("first question", str(prompts[0]))

    def test_empty_message_on_a_warm_session_launches_nothing(self):
        clients, prompts = self._run([(self._HISTORY, "prompt"), (self._EMPTY, "prompt")])
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(prompts), 1)

    _LOGGER = "omnigent.harnesses.claude_sdk_executor"
    _NEXT = [
        *_HISTORY,
        {"role": "assistant", "content": "the deck", "session_id": "s"},
        {"role": "user", "content": "and the notes", "session_id": "s"},
    ]

    def setUp(self):
        from omnigent.harnesses import claude_sdk_executor

        claude_sdk_executor._client_drop_reasons.clear()

    def _launches(self, logs) -> list[str]:
        return [r.getMessage() for r in logs.records if "Claude CLI launched" in r.getMessage()]

    def test_a_second_turn_with_nothing_changed_reuses_the_warm_cli(self):
        with self.assertLogs(self._LOGGER, level="INFO") as logs:
            clients, prompts = self._run(
                [(self._EMPTY, "prompt"), (self._HISTORY, "prompt"), (self._NEXT, "prompt")]
            )
        self.assertEqual(len(clients), 1, "no CLI launch after the pre-launch")
        self.assertEqual(prompts[1], "and the notes", "the warm CLI gets only the new message")
        launches = self._launches(logs)
        self.assertEqual(len(launches), 1)
        self.assertIn("pre-launch", launches[0])

    def test_a_relaunch_names_what_differs(self):
        with self.assertLogs(self._LOGGER, level="INFO") as logs:
            self._run([(self._EMPTY, "prompt"), (self._HISTORY, "new prompt")])
        self.assertIn("differs from the first turn in instructions", self._launches(logs)[-1])

    def test_a_warm_cli_without_the_checklist_is_relaunched_when_it_turns_on(self):
        from omnigent.harnesses.executor import ExecutorConfig

        with self.assertLogs(self._LOGGER, level="INFO") as logs:
            clients, prompts = self._run(
                [
                    (self._HISTORY, "prompt"),
                    (self._NEXT, "prompt", ExecutorConfig(checklist=True)),
                    (self._NEXT, "prompt", ExecutorConfig(checklist=True)),
                ]
            )
        self.assertEqual(len(clients), 2, "the flag relaunches once, then the CLI stays warm")
        self.assertEqual(clients[1].env.get("CLAUDE_CODE_ENABLE_TODO_TOOLS"), "1")
        self.assertIn("TaskCreate", clients[1].tools)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TODO_TOOLS", clients[0].env)
        self.assertIn("first question", str(prompts[1]), "the new CLI gets the whole history")
        self.assertIn("the turn's mode changed", self._launches(logs)[-1])

    def test_a_launch_after_an_interrupt_says_so(self):
        from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor

        clients: list = []
        prompts: list = []

        async def _t():
            with patch(
                "omnigent.harnesses.claude_sdk_executor._ensure_sdk",
                return_value=self._fake_sdk(clients, prompts),
            ):
                executor = ClaudeSDKExecutor()
                executor._force_close_client = AsyncMock()  # type: ignore[method-assign]
                _ = [e async for e in executor.run_turn(self._HISTORY, [], "prompt")]
                await executor.interrupt_session("s")
                # The adapter replaces its executor after an interrupt; the reason survives.
                _ = [e async for e in ClaudeSDKExecutor().run_turn(self._NEXT, [], "prompt")]

        with self.assertLogs(self._LOGGER, level="INFO") as logs:
            _run(_t())
        self.assertEqual(len(clients), 2)
        self.assertIn("the previous turn was interrupted", self._launches(logs)[-1])
