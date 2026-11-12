"""Projects: card parsing, the per-turn list, and the ``open_project`` tool."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    SUBAGENT_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.superchat import prompt_prefix
from omnigent.superchat.feature import HandlerCtx, ToolManagerCtx
from omnigent.superchat.features import FEATURE_HANDLERS
from omnigent.superchat.projects import FEATURE
from omnigent.superchat.projects.block import MAX_LISTED, open_project_slug, projects_block
from omnigent.superchat.projects.card import list_cards, parse_card, read_card, workspace_folder
from omnigent.superchat.projects.choice import (
    WORKSPACE_CHOSEN_LABEL_KEY,
    WORKSPACE_MOVED_LABEL_KEY,
    chosen_by_person,
    deliver_person_move,
)
from omnigent.superchat.projects.handlers import handle_open_project

CHAT = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME so ``~/workspace/projects`` is under the test's control."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "workspace" / "projects").mkdir(parents=True)
    return tmp_path / "workspace"


def _card(home: Path, slug: str, front: str | None = None, *, mtime: float | None = None) -> Path:
    folder = home / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    card = folder / "PROJECT.md"
    card.write_text(
        front
        if front is not None
        else f"---\nname: {slug.title()}\nsummary: About {slug}\n---\n\nNotes.\n"
    )
    if mtime is not None:
        os.utime(card, (mtime, mtime))
    return folder


# ── card parsing ───────────────────────────────────────────────────────────


def test_parse_full_card() -> None:
    card = parse_card(
        "q3-deck",
        "---\nname: Q3 board deck\naliases: [the deck, board slides]\nsummary: Deck for the\n"
        "  board\npeople: [Dana]\ngoal: board-prep\nupdated: 2026-10-01\n---\nnotes",
    )
    assert card is not None
    assert card.name == "Q3 board deck"
    assert card.aliases == ("the deck", "board slides")
    assert card.summary == "Deck for the board"
    assert card.people == ("Dana",)
    assert card.goal == "board-prep"
    assert card.updated == date(2026, 10, 1).isoformat()


@pytest.mark.parametrize(
    "text",
    [
        "no front matter",
        "---\nname: [unclosed\n---\n",
        "---\n- just\n- a list\n---\n",
        "---\nsummary: no name\n---\n",
        "---\nname:   \n---\n",
        "---\nname: never closed\n",
    ],
)
def test_unusable_front_matter_is_none(text: str) -> None:
    assert parse_card("x", text) is None


def test_malformed_optional_fields_are_ignored() -> None:
    card = parse_card("x", "---\nname: X\naliases: {a: b}\npeople: 3\ngoal: 7\n---\n")
    assert card is not None
    assert (card.aliases, card.people, card.goal) == ((), (), None)


def test_a_lone_string_alias_counts(home: Path) -> None:
    _card(home, "x", "---\nname: X\naliases: the thing\n---\n")
    card = read_card("x")
    assert card is not None and card.aliases == ("the thing",)


def test_read_card_refuses_non_slugs(home: Path) -> None:
    assert read_card("../escape") is None
    assert read_card("Has Caps") is None


# ── listing ────────────────────────────────────────────────────────────────


def test_list_is_newest_first_and_capped(home: Path) -> None:
    for i in range(5):
        _card(home, f"p{i}", mtime=1_000_000 + i)
    assert [c.slug for c in list_cards(3)] == ["p4", "p3", "p2"]


def test_bad_cards_and_stray_folders_are_skipped(home: Path) -> None:
    _card(home, "good", mtime=1_000_000)
    _card(home, "broken", "---\nname: [oops\n---\n", mtime=1_000_005)
    (home / "projects" / "no-card").mkdir()
    (home / "projects" / "Not A Slug").mkdir()
    (home / "projects" / "stray.txt").write_text("x")
    assert [c.slug for c in list_cards(15)] == ["good"]


def test_cap_counts_only_usable_cards(home: Path) -> None:
    """A broken newest card does not use up a slot."""
    _card(home, "a", mtime=1_000_001)
    _card(home, "b", mtime=1_000_000)
    _card(home, "broken", "nope", mtime=1_000_009)
    assert [c.slug for c in list_cards(2)] == ["a", "b"]


def test_missing_projects_dir_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list_cards(15) == []


# ── the per-turn block ─────────────────────────────────────────────────────


def test_no_projects_no_block(home: Path) -> None:
    assert projects_block(home) is None


def test_block_lists_name_aliases_summary_and_marks_the_open_one(home: Path) -> None:
    deck = _card(
        home,
        "q3-deck",
        "---\nname: Q3 board deck\naliases: [the deck]\nsummary: Deck for the board\n---\n",
    )
    _card(home, "taxes")
    block = projects_block(deck)
    assert block is not None
    assert "- q3-deck: Q3 board deck (also: the deck). Deck for the board [open now]" in block
    assert "- taxes: Taxes. About taxes\n" in block + "\n"
    assert block.count("[open now]") == 1
    assert "open_project" in block


def test_block_marks_nothing_at_the_workspace_root(home: Path) -> None:
    _card(home, "taxes")
    block = projects_block(home)
    assert block is not None and "[open now]" not in block


def test_a_subfolder_of_a_project_still_marks_it(home: Path) -> None:
    deck = _card(home, "q3-deck")
    sub = deck / "drafts"
    sub.mkdir()
    assert open_project_slug(sub) == "q3-deck"
    assert open_project_slug(home) is None
    assert open_project_slug(None) is None


def test_block_is_capped_but_always_includes_the_open_project(home: Path) -> None:
    for i in range(MAX_LISTED + 5):
        _card(home, f"p{i:02d}", mtime=1_000_000 + i)
    oldest = home / "projects" / "p00"
    block = projects_block(oldest)
    assert block is not None
    assert block.count("\n- ") == MAX_LISTED + 1
    assert "- p00: P00. About p00 [open now]" in block
    assert "p04" not in block


def test_long_summary_is_clipped(home: Path) -> None:
    _card(home, "x", f"---\nname: X\nsummary: {'w' * 400}\n---\n")
    block = projects_block(None)
    assert block is not None and "…" in block and "w" * 200 not in block


async def test_turn_prefix_carries_the_block_and_survives_failures(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _card(home, "taxes")
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", home)
    assert any(b.startswith("[Projects") for b in blocks)
    assert blocks[-1].startswith("[Current local time")  # local time still goes first

    def boom(_: Path | None) -> str:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(prompt_prefix, "projects_block", boom)
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", home)
    assert not any(b.startswith("[Projects") for b in blocks)


async def test_turn_prefix_without_projects_has_no_block(home: Path) -> None:
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", home)
    assert not any(b.startswith("[Projects") for b in blocks)


# ── open_project ───────────────────────────────────────────────────────────


class _Server:
    """Records ``PUT /v1/sessions/{id}/workspace`` and answers with a canned status.

    ``GET /v1/sessions/{id}`` returns ``labels``; ``PATCH`` label writes land in ``patches``.
    """

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.status = status
        self.body = body or {}
        self.labels = dict(labels or {})

    async def put(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append((url, json))
        return httpx.Response(self.status, json=self.body)

    async def get(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(200, json={"id": "conv_1", "labels": self.labels})

    async def patch(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.patches.append((url, json))
        self.labels.update(json["labels"])
        return httpx.Response(200, json={})


def _ctx(server: _Server | None) -> HandlerCtx:
    return HandlerCtx("open_project", server, "conv_1", CHAT)  # type: ignore[arg-type]


async def test_open_project_sets_the_workspace_and_returns_a_receipt(home: Path) -> None:
    folder = _card(home, "q3-deck", "---\nname: Q3 board deck\n---\n")
    server = _Server()
    out = json.loads(await handle_open_project(_ctx(server), {"slug": "q3-deck"}))
    assert server.calls == [
        (
            "/v1/sessions/conv_1/workspace",
            {"workspace": str(folder), "project_name": "Q3 board deck"},
        )
    ]
    assert out == {"opened": "q3-deck", "name": "Q3 board deck", "path": str(folder)}


async def test_open_project_without_a_card_is_refused(home: Path) -> None:
    (home / "projects" / "empty").mkdir()
    server = _Server()
    out = json.loads(await handle_open_project(_ctx(server), {"slug": "empty"}))
    assert "no Project 'empty'" in out["error"]
    assert server.calls == []


@pytest.mark.parametrize("slug", ["../..", "a/b", "", 7, "UP"])
async def test_open_project_refuses_anything_but_a_slug(home: Path, slug: object) -> None:
    server = _Server()
    out = json.loads(await handle_open_project(_ctx(server), {"slug": slug}))
    assert "error" in out
    assert server.calls == []


async def test_open_project_null_returns_to_the_workspace_root(home: Path) -> None:
    server = _Server()
    out = json.loads(await handle_open_project(_ctx(server), {"slug": None}))
    assert server.calls == [("/v1/sessions/conv_1/workspace", {"workspace": str(home)})]
    assert out == {"opened": None, "path": str(home)}


async def test_open_project_reports_the_servers_refusal(home: Path) -> None:
    _card(home, "q3-deck")
    server = _Server(400, {"error": {"message": "outside the agent's required path"}})
    out = json.loads(await handle_open_project(_ctx(server), {"slug": "q3-deck"}))
    assert "outside the agent's required path" in out["error"]


async def test_open_project_needs_server_access(home: Path) -> None:
    out = json.loads(await handle_open_project(_ctx(None), {"slug": None}))
    assert "server access" in out["error"]


# ── the person's choice ────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", ["q3-deck", None])
async def test_open_project_keeps_the_folder_the_person_chose(
    home: Path, slug: str | None
) -> None:
    _card(home, "q3-deck")
    server = _Server(labels={WORKSPACE_CHOSEN_LABEL_KEY: "person"})
    out = json.loads(await handle_open_project(_ctx(server), {"slug": slug}))
    assert server.calls == []
    assert out["opened"] is None
    assert "person chose" in out["refused"]


async def test_a_cleared_choice_lets_open_project_move_again(home: Path) -> None:
    _card(home, "q3-deck")
    server = _Server(labels={WORKSPACE_CHOSEN_LABEL_KEY: ""})
    out = json.loads(await handle_open_project(_ctx(server), {"slug": "q3-deck"}))
    assert out["opened"] == "q3-deck"
    assert len(server.calls) == 1


async def test_the_person_move_note_is_told_once(home: Path) -> None:
    server = _Server(labels={WORKSPACE_MOVED_LABEL_KEY: "/home/u/workspace/reports"})
    assert await deliver_person_move(server, "conv_1") == [  # type: ignore[arg-type]
        "The person moved this chat to /home/u/workspace/reports."
    ]
    assert server.patches == [("/v1/sessions/conv_1", {"labels": {WORKSPACE_MOVED_LABEL_KEY: ""}})]
    assert await deliver_person_move(server, "conv_1") == []  # type: ignore[arg-type]
    assert await deliver_person_move(None, "conv_1") == []


async def test_the_person_move_note_reaches_the_turn_prefix(home: Path) -> None:
    server = _Server(labels={WORKSPACE_MOVED_LABEL_KEY: "/home/u/workspace/reports"})
    blocks = await prompt_prefix.turn_prefix_blocks(server, "conv_1", home)  # type: ignore[arg-type]
    assert "The person moved this chat to /home/u/workspace/reports." in blocks


@pytest.mark.parametrize(
    ("labels", "chosen"),
    [({WORKSPACE_CHOSEN_LABEL_KEY: "person"}, True), ({WORKSPACE_CHOSEN_LABEL_KEY: ""}, False)],
)
def test_chosen_by_person_reads_the_mark(labels: dict[str, str], chosen: bool) -> None:
    assert chosen_by_person(labels) is chosen
    assert chosen_by_person(None) is False


@pytest.mark.parametrize(
    ("workspace", "folder"),
    [
        ("/home/u/workspace", None),
        ("/home/u/workspace/", None),
        ("/home/u/workspace/reports/q3", "reports/q3"),
        ("/home/u/workspace/projects/q3-deck/", "projects/q3-deck"),
        ("/tmp/elsewhere", None),
        (None, None),
    ],
)
def test_workspace_folder_is_relative_to_the_workspace(
    workspace: str | None, folder: str | None
) -> None:
    assert workspace_folder(workspace) == folder


# ── registration ───────────────────────────────────────────────────────────


def test_open_project_is_offered_to_the_muse_not_to_helpers() -> None:
    ctx = ToolManagerCtx()
    assert [t.name() for t in FEATURE.tools(CHAT, ctx)] == ["open_project"]
    assert FEATURE.tools({**CHAT, SUBAGENT_LABEL_KEY: "worker"}, ctx) == []
    assert FEATURE.tools(None, ctx) == []
    assert FEATURE_HANDLERS["open_project"] is handle_open_project


# ── a Project as the working directory ─────────────────────────────────────

REPO = Path(__file__).resolve().parents[3]
WORKSPACE = "/home/superchat/workspace"


@pytest.mark.parametrize("name", ["superchat-claude", "superchat-pi"])
def test_bundles_offer_open_project_and_keep_the_whole_workspace_reachable(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File tools are confined to the cwd; a Project cwd must not cut them off from the rest."""
    from omnigent.spec import parse

    monkeypatch.setenv("SUPERCHAT_CLAUDE_MODEL", "m")
    spec = parse(REPO / "engine/agents/bundles" / name)
    worker = next(a for a in spec.sub_agents if a.name == "worker")
    assert "open_project" in spec.tools.allow
    for agent in [spec, *spec.sub_agents, *worker.sub_agents]:
        if agent.os_env is None:
            # A Type with no shell or file tools (the notes passes) needs no workspace grant.
            assert not any(tool.startswith("sys_os") for tool in agent.tools.allow), agent.name
            continue
        assert agent.os_env.sandbox.write_paths == [WORKSPACE], agent.name


async def test_file_tools_in_a_project_still_reach_the_workspace(home: Path) -> None:
    from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.harnesses.os_env import create_os_environment

    project = _card(home, "q3-deck")
    other = home / "your_files"
    other.mkdir()

    def env(grants: list[str] | None) -> Any:
        sandbox = OSEnvSandboxSpec(type="none", write_paths=grants)
        return create_os_environment(
            OSEnvSpec(type="caller_process", cwd=str(project), sandbox=sandbox)
        )

    target = str(other / "report.md")
    refused = await env(None).write(path=target, content="x")
    assert "outside the" in str(refused)
    assert not (other / "report.md").exists()
    await env([str(home)]).write(path=target, content="x")
    assert (other / "report.md").read_text() == "x"


def test_block_lists_top_level_file_names_capped_without_recursing(home: Path) -> None:
    folder = _card(home, "deck")
    for i in range(10):
        (folder / f"f{i:02d}.md").write_text("x")
    (folder / ".hidden").write_text("x")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.md").write_text("x")
    block = projects_block(None)
    assert block is not None
    assert (
        "Files: f00.md, f01.md, f02.md, f03.md, f04.md, f05.md, f06.md, f07.md (+3 more)" in block
    )
    assert "PROJECT.md" not in block.split("Files:")[1]
    assert ".hidden" not in block and "deep.md" not in block


def test_block_tells_the_model_to_match_every_project(home: Path) -> None:
    _card(home, "taxes")
    block = projects_block(None)
    assert block is not None
    header = " ".join(block.split("\n")[0].split())
    assert "EVERY Project" in header and "not just the open one" in header
    assert "ask which" in header and "edit nothing" in header


async def test_project_list_reaches_the_prompt_of_claude_sdk_and_pi(home: Path) -> None:
    """The per-turn list is in the text each harness sends the model, in both turn shapes."""
    from omnigent.harnesses.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.harnesses.pi_executor import _build_pi_prompt
    from omnigent.runner.app import _prepend_turn_blocks

    _card(home, "taxes")
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", home)
    flat = {"content": [{"type": "input_text", "text": "what is in taxes?"}]}
    history = {
        "content": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "yo"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "what is in taxes?"}],
            },
        ]
    }

    def flatten(prompt: object) -> str:
        return prompt if isinstance(prompt, str) else json.dumps(prompt)

    for body in (flat, history):
        sent = _prepend_turn_blocks(body, blocks, "conv")["content"]
        messages = (
            sent
            if sent and sent[0].get("type") == "message"
            else [{"role": "user", "content": sent}]
        )
        for first in (True, False):
            assert "- taxes: Taxes. About taxes" in flatten(
                _build_pi_prompt(messages, is_first_turn=first)
            )
        for resume in (True, False):
            assert "- taxes: Taxes. About taxes" in flatten(
                ClaudeSDKExecutor._build_prompt(messages, resume_session=resume)
            )


@pytest.mark.parametrize(
    ("workspace", "slug"),
    [
        ("/home/superchat/workspace/projects/q3-deck", "q3-deck"),
        ("/home/superchat/workspace/projects/q3-deck/drafts", "q3-deck"),
        ("/home/superchat/workspace", None),
        ("/home/superchat/workspace/projects", None),
        ("/home/superchat/workspace/projects/UP", None),
        (None, None),
    ],
)
def test_project_slug_is_read_from_the_working_directory(
    workspace: str | None, slug: str | None
) -> None:
    from omnigent.superchat.projects.card import project_slug_from_workspace

    assert project_slug_from_workspace(workspace) == slug
