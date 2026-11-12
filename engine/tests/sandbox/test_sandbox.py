"""
Tests for the sandbox wrapper package.

The wrapper is pure re-exports of the existing
``omnigent.harnesses.sandbox`` surface, so the tests only verify two
properties: every name the ``__all__`` lists is importable, and the
wrapped names are the same Python objects as their inner
counterparts. Behavioral tests for the underlying implementation live
in ``tests/harnesses/`` and continue to exercise ``harnesses.sandbox``
directly.
"""

from __future__ import annotations

import omnigent.harnesses.sandbox as inner_sandbox
from omnigent import sandbox


def test_sandbox_all_symbols_importable() -> None:
    """
    Every name in ``omnigent.sandbox.__all__`` resolves to an attribute on
    the wrapper module.

    Catches accidental drift between the ``__all__`` list and the actual
    re-export — the failure mode is "looks fine in the source, breaks at
    `from omnigent.sandbox import X`".
    """
    for name in sandbox.__all__:
        assert hasattr(sandbox, name), f"omnigent.sandbox missing re-export {name!r}"


def test_sandbox_reexports_are_inner_objects() -> None:
    """
    The re-exported symbols are the same Python objects as the originals
    in ``omnigent.harnesses.sandbox``.

    Identity (``is``) — not equality — because anything else means the
    wrapper has accidentally introduced a parallel definition. Drift here
    would silently break behavior since callers using either import path
    would get different classes and ``isinstance`` checks would fail.
    """
    assert sandbox.SandboxPolicy is inner_sandbox.SandboxPolicy
    assert sandbox.SandboxBackend is inner_sandbox.SandboxBackend
    assert sandbox.resolve_sandbox is inner_sandbox.resolve_sandbox
    assert sandbox.activate_sandbox is inner_sandbox.activate_sandbox
    assert sandbox.register_backend is inner_sandbox.register_backend
    assert sandbox.with_additional_read_roots is inner_sandbox.with_additional_read_roots
    assert sandbox.with_additional_write_files is inner_sandbox.with_additional_write_files
    assert sandbox.with_additional_write_roots is inner_sandbox.with_additional_write_roots
    assert sandbox.create_private_tmpdir is inner_sandbox.create_private_tmpdir
    assert sandbox.cleanup_private_tmpdir is inner_sandbox.cleanup_private_tmpdir
    assert sandbox.set_temp_env is inner_sandbox.set_temp_env
    assert sandbox.run_launcher is inner_sandbox.run_launcher
    assert sandbox.create_exec_launcher is inner_sandbox.create_exec_launcher


def test_default_sandbox_for_platform_is_bwrap_on_linux_regardless_of_binary() -> None:
    """
    The Linux platform default is ``linux_bwrap`` and the choice does
    **not** depend on whether the ``bwrap`` binary is installed on the
    host doing the resolving.

    This is the property that keeps spec parsing host-independent: the
    default is materialized at parse time (loader / spec parser), so a
    CI node or dev laptop without bubblewrap must still be able to load
    a YAML whose ``sandbox:`` block omits ``type:``. The fail-loud
    "no usable mechanism" check is deferred to the backend's runtime
    ``resolve()`` (covered by
    ``tests/harnesses/test_bwrap_sandbox.py::test_resolve_raises_when_bwrap_missing``).
    We patch ``shutil.which`` to ``None`` to prove a missing binary
    does not change the selected type.
    """
    import sys
    from unittest.mock import patch

    from omnigent.harnesses.datamodel import OSEnvSandboxSpec

    with (
        patch.object(sys, "platform", "linux"),
        patch("omnigent.harnesses.sandbox.shutil.which", return_value=None),
    ):
        spec = inner_sandbox._default_sandbox_for_platform()
    assert isinstance(spec, OSEnvSandboxSpec)
    assert spec.type == "linux_bwrap", (
        "Linux default sandbox should be 'linux_bwrap' independent of "
        f"bwrap availability; got {spec.type!r}."
    )
