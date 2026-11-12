"""In-host side of the engine model proxy (:mod:`omnigent.superchat.models.proxy`).

The owner's model API key never enters the host. The harness is pointed at the engine's proxy
and its gateway auth command (``python3 -m omnigent.host.model_credential token``) prints the only
credential the proxy accepts: this host's own launch token, as ``<host_id>:<token>``. The token
is a lesser, expiring credential already present in the host (the Databricks and git brokers
persist it the same way); it only lets the host use the proxy as its owner, and stops working
when the host row is deleted.

:func:`configure_host_model` runs at ``omnigent host`` startup (managed sandboxes only) and records
those coordinates in a private ``0600`` sidecar the auth command reads.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from omnigent.host.identity import HOST_TOKEN_ENV_VAR

_SIDECAR_NAME = ".omnigent-model-proxy.json"


def _sidecar_path() -> Path:
    return Path.home() / _SIDECAR_NAME


def configure_host_model(host_id: str, path: Path | None = None) -> bool:
    """Persist ``host_id`` + launch token (0600) for the auth command (sandbox only).

    :returns: ``True`` when the sidecar was written.
    """
    if os.environ.get("IS_SANDBOX") != "1":
        return False
    token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if not token or not host_id:
        return False
    target = path or _sidecar_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"host_id": host_id, "host_token": token}, handle)
    except OSError:
        return False
    return True


def read_credential(path: Path | None = None) -> str | None:
    """:returns: ``"<host_id>:<token>"`` from the sidecar, or ``None`` when absent or malformed."""
    try:
        data = json.loads((path or _sidecar_path()).read_text(encoding="utf-8"))
        host_id, token = data["host_id"], data["host_token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(host_id, str) or not isinstance(token, str) or not host_id or not token:
        return None
    return f"{host_id}:{token}"


def main(argv: list[str] | None = None) -> int:
    """Print the proxy credential on stdout (nothing when unconfigured, so auth fails cleanly)."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] != "token":
        return 0
    credential = read_credential()
    if credential:
        sys.stdout.write(credential + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
