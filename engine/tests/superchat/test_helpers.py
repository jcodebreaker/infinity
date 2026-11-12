"""start_helper: schema, who is offered it, title, dedup, translation, files in the result."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    SUBAGENT_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.superchat.cards.tools import build_card
from omnigent.superchat.feature import (
    HandlerCtx,
    SpawnRequest,
    SpawnResult,
    SubAgentHost,
    ToolManagerCtx,
)
from omnigent.superchat.features import notify_result
from omnigent.superchat.helpers import handlers as h
from omnigent.superchat.helpers import saved_files as sf
from omnigent.superchat.helpers.feature import HELPERS_FEATURE
from omnigent.superchat.helpers.tools import MessageHelperTool, StartHelperTool
from omnigent.superchat.models.tiers import CATEGORIES
from omnigent.superchat.subagents import (
    HELPER_EFFORT_CHOICES,
    format_subagent_wake_notice_with_result,
    result_notice_reminder,
)

CHAT = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
REPO = Path(__file__).resolve().parents[3]


def _spec(*names: str) -> Any:
    return SimpleNamespace(sub_agents=[SimpleNamespace(name=n) for n in names])


def _offered(labels: dict[str, str] | None, spec: Any) -> list[str]:
    return [t.name() for t in HELPERS_FEATURE.tools(labels, ToolManagerCtx(spec=spec))]


# ── schema and surface ─────────────────────────────────────────────────────


def test_schema_is_one_flat_task_with_a_model_category() -> None:
    params = StartHelperTool().get_schema()["function"]["parameters"]
    assert params["required"] == ["task"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {
        "task",
        "title",
        "model",
        "reasoning",
        "files",
        "project",
        "path",
    }
    assert params["properties"]["model"]["enum"] == ["foundation", "builder", "light"]
    assert params["properties"]["model"]["default"] == "builder"
    assert params["properties"]["reasoning"]["enum"] == ["low", "medium", "high"]


def test_muse_and_worker_get_it_subworker_does_not() -> None:
    assert _offered(CHAT, _spec("worker", "goal", "teacher")) == ["start_helper", "message_helper"]
    assert h.helper_type_for(_spec("worker", "goal", "teacher")) == "worker"
    assert _offered(CHAT, _spec("subworker")) == ["start_helper", "message_helper"]
    assert h.helper_type_for(_spec("subworker")) == "subworker"
    assert _offered(CHAT, _spec()) == []
    assert _offered(None, _spec("worker")) == []


def test_a_type_declares_its_role_in_params() -> None:
    spec = SimpleNamespace(
        sub_agents=[
            SimpleNamespace(name="doer", params={"helper_type": "worker"}),
            SimpleNamespace(name="leaf", params={"helper_type": "subworker"}),
            SimpleNamespace(name="other", params={}),
        ]
    )
    assert h.helper_roles_for(spec) == {"doer": "worker", "leaf": "subworker"}
    assert h.helper_type_for(spec) == "doer"
    assert h.helper_type_for(SimpleNamespace(sub_agents=[spec.sub_agents[1]])) == "leaf"
    assert h.helper_role("leaf", {"leaf": "subworker"}) == "subworker"
    # an undeclared or invalid role falls back to the literal name
    bad = SimpleNamespace(sub_agents=[SimpleNamespace(name="worker", params={"helper_type": "x"})])
    assert h.helper_type_for(bad) == "worker"


def test_bundles_declare_the_roles() -> None:
    for name in ("superchat-claude", "superchat-pi"):
        worker = yaml.safe_load(
            (REPO / f"engine/agents/bundles/{name}/agents/worker/config.yaml").read_text()
        )
        sub = yaml.safe_load(
            (
                REPO / f"engine/agents/bundles/{name}/agents/worker/agents/subworker/config.yaml"
            ).read_text()
        )
        assert worker["params"] == {"helper_type": "worker"}
        assert sub["params"] == {"helper_type": "subworker"}


def test_bundle_surfaces() -> None:
    bundle = REPO / "engine/agents/bundles/superchat-claude"
    muse = yaml.safe_load((bundle / "config.yaml").read_text())["tools"]["allow"]
    worker = yaml.safe_load((bundle / "agents/worker/config.yaml").read_text())["tools"]["allow"]
    sub = yaml.safe_load((bundle / "agents/worker/agents/subworker/config.yaml").read_text())[
        "tools"
    ]["allow"]
    assert "start_helper" in muse
    assert "message_helper" in muse
    assert "sys_session_send" not in muse
    # A Helper's result arrives as a message: no inbox or session polling (ADR 0029).
    assert not {"sys_read_inbox", "sys_session_list", "sys_session_get_info"} & set(muse)
    assert "start_helper" in worker
    assert "sys_session_send" not in worker
    assert "start_helper" not in sub


# ── title ──────────────────────────────────────────────────────────────────


def test_title_is_first_words_in_sentence_case_never_a_slug() -> None:
    title = h.helper_title("compare notes apps: Notion vs Obsidian vs Bear for a team", set())
    assert title is not None
    assert title[0].isupper()
    assert "-" not in title.split()[0]
    assert ":" not in title
    assert len(title) <= 48


def test_given_title_wins_over_the_brief() -> None:
    brief = "The person wants using Python in the PDFs folder to count words"
    title = h.helper_title(brief, set(), "counting words in your PDFs.")
    assert title == "Counting words in your PDFs"


@pytest.mark.parametrize(
    "brief",
    [
        "The person wants using Python in the PDFs folder to count words",
        "The user asked for a comparison of notes apps",
        "Your task is to compare notes apps",
        "You are a researcher comparing notes apps",
    ],
)
def test_brief_speak_is_never_a_title(brief: str) -> None:
    assert h.helper_title(brief, set()) is None


def test_title_is_unique_among_siblings() -> None:
    assert h.helper_title("Compare notes apps", {"compare notes apps"}) == "Compare notes apps 2"
    assert h.helper_title("Compare notes apps", set()) == "Compare notes apps"


# ── handler: translation and dedup ─────────────────────────────────────────


def _host(
    spec: Any,
    calls: list[SpawnRequest],
    rows: list[str] | None = None,
    refuse: str | None = None,
) -> SubAgentHost:
    async def spawn(request: SpawnRequest) -> SpawnResult:
        calls.append(request)
        await asyncio.sleep(0.01)
        return SpawnResult(error=refuse) if refuse else SpawnResult(child_id=f"conv_{len(calls)}")

    async def titles() -> list[str]:
        return rows or []

    return SubAgentHost(
        declared_types=tuple(sub.name for sub in spec.sub_agents),
        child_titles=titles,
        spawn=spawn,
        helper_roles=h.helper_roles_for(spec),
    )


@pytest.fixture
def dispatched() -> list[SpawnRequest]:
    h._recent.clear()
    h._locks.clear()
    return []


def _ctx(
    calls: list[SpawnRequest], spec: Any = None, conv: str = "conv_chat", **host_kw: Any
) -> HandlerCtx:
    return HandlerCtx(
        "start_helper",
        object(),  # type: ignore[arg-type]
        conv,
        CHAT,
        sub_agents=_host(spec or _spec("worker"), calls, **host_kw),
    )


async def test_receipt_text_differs_by_caller(dispatched: list[SpawnRequest]) -> None:
    muse = json.loads(
        await h.handle_start_helper(_ctx(dispatched), {"task": "Compare notes apps"})
    )
    assert "end your turn" in muse["message"]
    assert "one sentence" in muse["message"]
    part = json.loads(
        await h.handle_start_helper(
            _ctx(dispatched, _spec("subworker"), "conv_coord"), {"task": "Qdrant"}
        )
    )
    assert "tell the person" not in part["message"].lower()
    assert "write the deliverable yourself" in part["message"]
    assert "one sentence" not in part["message"]


async def test_call_becomes_the_create_path_request(dispatched: list[SpawnRequest]) -> None:
    out = await h.handle_start_helper(
        _ctx(dispatched),
        {"task": "Compare notes apps", "reasoning": "high", "files": ["file_1"]},
    )
    receipt = json.loads(out)
    assert receipt["started"] is True
    assert receipt["helper_id"] == "conv_1"
    assert "end your turn" in receipt["message"]
    assert dispatched == [
        SpawnRequest(
            agent="worker",
            task="Compare notes apps",
            title="Compare notes apps",
            model="builder",
            reasoning_effort="high",
            file_ids=("file_1",),
        )
    ]


async def test_title_avoids_existing_children(dispatched: list[SpawnRequest]) -> None:
    await h.handle_start_helper(
        _ctx(dispatched, rows=["worker: Compare notes apps"]), {"task": "Compare notes apps"}
    )
    assert dispatched[0].title == "Compare notes apps 2"


async def test_coordinator_starts_its_subworker(dispatched: list[SpawnRequest]) -> None:
    await h.handle_start_helper(
        _ctx(dispatched, _spec("subworker")), {"task": "Part one", "model": "light"}
    )
    assert (dispatched[0].agent, dispatched[0].model) == ("subworker", "light")
    # An earlier name still starts the Helper, for one release.
    await h.handle_start_helper(
        _ctx(dispatched, _spec("subworker")), {"task": "Two", "model": "fast"}
    )
    assert dispatched[1].model == "fast"


async def test_same_task_twice_in_parallel_starts_one_helper(
    dispatched: list[SpawnRequest],
) -> None:
    a, b = await asyncio.gather(
        h.handle_start_helper(_ctx(dispatched), {"task": "Compare notes apps"}),
        h.handle_start_helper(_ctx(dispatched), {"task": "  compare   NOTES apps "}),
    )
    assert a == b
    assert len(dispatched) == 1


async def test_other_task_or_other_chat_or_later_starts_another(
    dispatched: list[SpawnRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.handle_start_helper(_ctx(dispatched), {"task": "One"})
    await h.handle_start_helper(_ctx(dispatched), {"task": "Two"})
    await h.handle_start_helper(_ctx(dispatched, conv="conv_other"), {"task": "One"})
    assert len(dispatched) == 3
    monkeypatch.setattr(h, "DEDUP_WINDOW_S", -1.0)
    await h.handle_start_helper(_ctx(dispatched), {"task": "One"})
    assert len(dispatched) == 4


async def test_stale_dedup_entries_and_idle_chat_locks_are_pruned(
    dispatched: list[SpawnRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.handle_start_helper(_ctx(dispatched, conv="conv_old"), {"task": "One"})
    assert "conv_old" in h._locks
    monkeypatch.setattr(h, "DEDUP_WINDOW_S", -1.0)
    await h.handle_start_helper(_ctx(dispatched, conv="conv_new"), {"task": "One"})
    assert set(h._locks) == {"conv_new"}
    assert {chat for chat, _ in h._recent} == {"conv_new"}


async def test_refusal_is_not_cached_and_names_the_right_tool(
    dispatched: list[SpawnRequest],
) -> None:
    out = await h.handle_start_helper(
        _ctx(dispatched, refuse="sub-agent concurrency cap reached"), {"task": "One"}
    )
    assert out == "Error: start_helper: sub-agent concurrency cap reached"
    assert not h._recent


@pytest.mark.parametrize(
    ("args", "mention"),
    [
        ({"task": "x", "colour": "y"}, "does not take colour"),
        ({"task": "x", "title": 3}, "'title' must be a string"),
        ({"task": " "}, "requires a non-empty 'task'"),
        ({"task": "x", "model": "gpt"}, "'model' must be one of foundation, builder, light"),
        ({"task": "x", "reasoning": "ultra"}, "'reasoning' must be one of low, medium, high"),
        ({"task": "x", "files": "file_1"}, "'files' must be a list of uploaded file ids"),
    ],
)
async def test_bad_input_is_refused_up_front_in_start_helper_terms(
    dispatched: list[SpawnRequest], args: dict[str, Any], mention: str
) -> None:
    out = await h.handle_start_helper(_ctx(dispatched), args)
    assert out == f"Error: start_helper: {mention}"
    assert not dispatched


# ── where the Helper works ─────────────────────────────────────────────────


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with a Project ``q3-deck`` and a plain folder ``reports/q3``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "workspace"
    (root / "projects" / "q3-deck").mkdir(parents=True)
    (root / "projects" / "q3-deck" / "PROJECT.md").write_text("---\nname: Q3 board deck\n---\n")
    (root / "reports" / "q3").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    return root.resolve()


async def test_project_starts_the_helper_in_its_folder(
    dispatched: list[SpawnRequest], workspace: Path
) -> None:
    await h.handle_start_helper(_ctx(dispatched), {"task": "Draft", "project": "q3-deck"})
    request = dispatched[0]
    assert Path(request.workspace or "").resolve() == workspace / "projects" / "q3-deck"
    assert request.project_name == "Q3 board deck"


async def test_path_starts_the_helper_in_that_folder(
    dispatched: list[SpawnRequest], workspace: Path
) -> None:
    await h.handle_start_helper(_ctx(dispatched), {"task": "Sum", "path": "reports/q3/"})
    assert (dispatched[0].workspace, dispatched[0].project_name) == (
        str(workspace / "reports" / "q3"),
        None,
    )


async def test_neither_keeps_the_callers_folder(
    dispatched: list[SpawnRequest], workspace: Path
) -> None:
    """No folder in the request: the child inherits the caller's current one at create."""
    await h.handle_start_helper(_ctx(dispatched), {"task": "Sum"})
    assert (dispatched[0].workspace, dispatched[0].project_name) == (None, None)


@pytest.mark.parametrize(
    ("args", "mention"),
    [
        ({"project": "nope"}, "no Project 'nope'"),
        ({"project": "../x"}, "no Project '../x'"),
        ({"path": "reports/q4"}, "no folder 'reports/q4' in the workspace"),
        ({"path": "../outside"}, "'path' must be a folder inside the workspace"),
        ({"path": "/etc"}, "'path' must be a folder inside the workspace"),
        ({"path": "projects/q3-deck/PROJECT.md"}, "no folder"),
        ({"project": "q3-deck", "path": "reports"}, "takes 'project' or 'path', not both"),
    ],
)
async def test_a_bad_folder_is_refused_before_any_helper_starts(
    dispatched: list[SpawnRequest], workspace: Path, args: dict[str, Any], mention: str
) -> None:
    out = await h.handle_start_helper(_ctx(dispatched), {"task": "x", **args})
    assert out.startswith(f"Error: start_helper: {mention}")
    assert not dispatched


async def test_a_symlink_out_of_the_workspace_is_refused(
    dispatched: list[SpawnRequest], workspace: Path
) -> None:
    (workspace / "link").symlink_to(workspace.parent / "outside")
    out = await h.handle_start_helper(_ctx(dispatched), {"task": "x", "path": "link"})
    assert out == "Error: start_helper: 'path' must be a folder inside the workspace"
    assert not dispatched


def test_tool_schema_uses_the_shared_choices() -> None:
    props = StartHelperTool().get_schema()["function"]["parameters"]["properties"]
    assert tuple(props["model"]["enum"]) == CATEGORIES
    assert tuple(props["reasoning"]["enum"]) == HELPER_EFFORT_CHOICES


# ── the result carries the Helper's files ──────────────────────────────────


def _save(session: str, artifact_id: str, name: str, version: int = 1) -> None:
    sf.record_saved_file(
        session,
        json.dumps({"type": "artifact", "id": artifact_id, "name": name, "version": version}),
    )


def test_files_of_helper_and_its_helpers_reach_the_notice() -> None:
    sf._saved.clear()
    _save("conv_worker", "a1", "report.html")
    _save("conv_sub", "a2", "table.csv")
    sf.record_saved_file("conv_worker", json.dumps({"error": "nope"}))
    tree = {"conv_worker": ["conv_sub"]}
    files = sf.collect_saved_files("conv_worker", lambda s: tree.get(s, []))
    assert {f["id"] for f in files} == {"a1", "a2"}
    notice = format_subagent_wake_notice_with_result(
        agent="worker",
        title="Compare",
        status="completed",
        child_session_id="conv_worker",
        result_text="Done",
        saved_files=sf.format_saved_files(files),
    )
    assert "artifact_id a1" in notice
    assert "artifact_id a2" in notice
    assert "report.html" in notice
    assert "never without its artifact_id" in (result_notice_reminder([notice]) or "")


def test_resave_keeps_newest_version_and_no_files_adds_nothing() -> None:
    sf._saved.clear()
    _save("conv_w", "a1", "r.html")
    _save("conv_w", "a9", "r.html", version=2)
    files = sf.collect_saved_files("conv_w", lambda _s: [])
    assert [f["id"] for f in files] == ["a9"]
    assert sf.format_saved_files([]) == ""


def test_file_card_accepts_the_artifact_id() -> None:
    card = build_card(
        {
            "card": "file",
            "data": {"name": "r.html", "artifactId": "a1", "version": 2},
            "fallback": "Saved r.html",
        }
    )
    assert card["data"]["artifactId"] == "a1"


def test_forget_drops_the_helper_and_its_helpers_files() -> None:
    sf._saved.clear()
    _save("conv_worker", "a1", "report.html")
    _save("conv_sub", "a2", "table.csv")
    _save("conv_other", "a3", "keep.csv")
    tree = {"conv_worker": ["conv_sub"]}
    sf.forget_saved_files("conv_worker", lambda s: tree.get(s, []))
    assert set(sf._saved) == {"conv_other"}


async def test_only_a_helpers_artifact_save_is_noted_by_the_feature_listener() -> None:
    sf._saved.clear()
    saved = json.dumps({"type": "artifact", "id": "a1", "name": "r.html"})
    helper = {**CHAT, SUBAGENT_LABEL_KEY: "worker"}

    def ctx(tool: str, labels: dict[str, str]) -> HandlerCtx:
        return HandlerCtx(tool, None, "conv_w", labels)

    notify_result(ctx("artifact_save", CHAT), saved)  # the Muse itself
    notify_result(ctx("artifact_list", helper), saved)  # another tool
    assert not sf._saved
    notify_result(ctx("artifact_save", helper), saved)
    assert sf._saved["conv_w"]["r.html"]["id"] == "a1"


def test_deleting_a_session_forgets_its_saved_files() -> None:
    from omnigent.runner.app import unregister_subagent_work_for_session

    sf._saved.clear()
    _save("conv_gone", "a1", "r.html")
    unregister_subagent_work_for_session("conv_gone")
    assert not sf._saved


# ── message_helper: pass a steer on to a running Helper ────────────────────


def _messaging_ctx(
    sent: list[tuple[str, str]],
    running: list[tuple[str, str]],
    reply: str | None = None,
) -> HandlerCtx:
    async def send(child_id: str, text: str) -> str:
        sent.append((child_id, text))
        return (
            reply if reply is not None else json.dumps({"task_id": child_id, "status": "running"})
        )

    async def live() -> list[tuple[str, str]]:
        return running

    async def titles() -> list[str]:
        return []

    async def spawn(request: SpawnRequest) -> SpawnResult:
        return SpawnResult(error="unused")

    host = SubAgentHost(
        declared_types=("worker",), child_titles=titles, spawn=spawn, send=send, running=live
    )
    return HandlerCtx("message_helper", object(), "conv_chat", CHAT, sub_agents=host)  # type: ignore[arg-type]


def test_message_helper_schema_takes_a_message_and_an_optional_id() -> None:
    params = MessageHelperTool().get_schema()["function"]["parameters"]
    assert params["required"] == ["message"]
    assert set(params["properties"]) == {"message", "helper_id"}
    assert params["additionalProperties"] is False


async def test_the_message_reaches_the_named_running_helper() -> None:
    sent: list[tuple[str, str]] = []
    ctx = _messaging_ctx(sent, [("conv_h1", "worker:Deck"), ("conv_h2", "worker:Sheet")])
    out = json.loads(
        await h.handle_message_helper(ctx, {"message": "make it blue", "helper_id": "conv_h1"})
    )
    assert sent == [("conv_h1", "make it blue")]
    assert out["sent"] is True and out["helper_id"] == "conv_h1"
    assert "Do not start another Helper" in out["message"]


async def test_one_running_helper_needs_no_id_and_several_must_be_told_apart() -> None:
    sent: list[tuple[str, str]] = []
    one = _messaging_ctx(sent, [("conv_h1", "worker:Deck")])
    assert json.loads(await h.handle_message_helper(one, {"message": "blue"}))["sent"] is True
    assert sent == [("conv_h1", "blue")]

    many = _messaging_ctx(sent, [("conv_h1", "worker:Deck"), ("conv_h2", "worker:Sheet")])
    refused = await h.handle_message_helper(many, {"message": "blue"})
    assert refused.startswith("Error: message_helper: several Helpers are running")
    assert "conv_h1" in refused and "conv_h2" in refused
    assert len(sent) == 1  # nothing was sent for the ambiguous call


async def test_no_message_goes_to_a_helper_that_is_not_running() -> None:
    sent: list[tuple[str, str]] = []
    none = _messaging_ctx(sent, [])
    assert "no Helper is running" in await h.handle_message_helper(none, {"message": "x"})
    other = _messaging_ctx(sent, [("conv_h1", "worker:Deck")])
    assert "not running" in await h.handle_message_helper(
        other, {"message": "x", "helper_id": "conv_gone"}
    )
    assert sent == []


async def test_bad_input_and_a_failed_send_are_refused_in_message_helper_terms() -> None:
    sent: list[tuple[str, str]] = []
    ctx = _messaging_ctx(
        sent, [("conv_h1", "worker:Deck")], reply="Error: sys_session_send failed to look up"
    )
    assert "non-empty 'message'" in await h.handle_message_helper(ctx, {"message": " "})
    assert "takes only" in await h.handle_message_helper(ctx, {"message": "x", "why": 1})
    failed = await h.handle_message_helper(ctx, {"message": "x"})
    assert failed.startswith("Error: message_helper: failed to look up")
    no_host = HandlerCtx("message_helper", None, "conv_chat", CHAT)
    assert "requires server access" in await h.handle_message_helper(no_host, {"message": "x"})


async def test_a_helper_that_finished_before_the_note_arrived_refuses_it() -> None:
    # Listed as running when the tool looked, idle by the time the send reached it: the send
    # itself refuses, so the note never starts a second task.
    sent: list[tuple[str, str]] = []
    ctx = _messaging_ctx(
        sent,
        [("conv_h1", "worker:Deck")],
        reply=json.dumps({"error": "not_running", "conversation_id": "conv_h1"}),
    )
    refused = await h.handle_message_helper(ctx, {"message": "blue"})
    assert refused == (
        "Error: message_helper: that Helper is not running; its result has arrived or will"
    )
    assert sent == [("conv_h1", "blue")]
