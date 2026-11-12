"""A session status edge carries the runner's turn number, naming the turn that ended."""

from __future__ import annotations

from unittest.mock import patch

from omnigent.server.routes._sessions import helpers


def test_a_status_edge_carries_the_runner_turn_number_and_omits_it_when_unknown() -> None:
    with patch.object(helpers.session_stream, "publish") as publish:
        helpers._publish_status("conv_turn", "idle", turn=42, persist_live_status=False)
        helpers._publish_status("conv_turn", "running", persist_live_status=False)
    numbered, unnumbered = (call.args[1] for call in publish.call_args_list)
    assert (numbered["status"], numbered["turn"]) == ("idle", 42)
    assert "turn" not in unnumbered
