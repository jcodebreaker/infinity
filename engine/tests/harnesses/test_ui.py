"""Tests for the shared CLI styling layer (``omnigent.harnesses.ui``)."""

from __future__ import annotations

import pytest

from omnigent.harnesses import ui


def test_accent_is_brand_magenta() -> None:
    """The shared accent is the Omnigent brand magenta."""

    assert ui.ACCENT == "#F43BA6"


def test_warnings_and_errors_go_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """Diagnostics print to stderr so piped stdout stays clean."""

    ui.warn("tmux not found")
    ui.error("uv is required")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tmux not found" in captured.err
    assert "uv is required" in captured.err


def test_status_lines_go_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Normal status (step/success/info) prints to stdout."""

    ui.step("Installing Omnigent")
    ui.success("Verified omnigent")
    ui.info("Using ~/.omnigent")
    captured = capsys.readouterr()
    assert "Installing Omnigent" in captured.out
    assert "✓ Verified omnigent" in captured.out
    assert captured.err == ""


def test_status_lines_are_plain_off_tty(capsys: pytest.CaptureFixture[str]) -> None:
    """Off a TTY (captured streams) output carries no ANSI escapes."""

    ui.success("done")
    ui.error("nope")
    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out
    assert "\x1b[" not in captured.err


def test_message_with_brackets_is_not_treated_as_markup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A message containing ``[...]`` is emitted verbatim, not as markup."""

    ui.success("installed [databricks] extra")
    captured = capsys.readouterr()
    assert "[databricks]" in captured.out
