"""Who an eval plays as (docs/evals.md): one eval person per run, recognised by email.

Each run plays as ``eval+<run id>@<eval domain>``, where the domain is that of
``SUPERCHAT_EVAL_EMAIL`` (default ``evals@superchat.local``). That address itself is the shared
eval person, used outside a run. The email is the marker: in header and OIDC modes it is the
user id, so :func:`is_eval_identity` needs no table and every people-facing feature checks it
before it spends on, lists or messages a person.

Kept free of engine imports so any capability (the host launch, the Lookout, admin) can use it.
"""

from __future__ import annotations

import os
import re

DEFAULT_EMAIL = "evals@superchat.local"
#: Where an eval person's host writes each model call's context record (workspace-relative; the
#: runner resolves it against its workspace, and an eval service reads it over the filesystem
#: routes). Read by :mod:`omnigent.harnesses.llm_trace` from ``OMNIGENT_LLM_CONTEXT_DIR``.
CONTEXT_PATH = ".omnigent/llm-context"
CONTEXT_DIR_ENV = "OMNIGENT_LLM_CONTEXT_DIR"
_RUN_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def base_email() -> str:
    """The eval person outside a run: ``SUPERCHAT_EVAL_EMAIL``, else the default."""
    return os.environ.get("SUPERCHAT_EVAL_EMAIL", "").strip() or DEFAULT_EMAIL


def eval_domain() -> str:
    return base_email().rpartition("@")[2].lower() or DEFAULT_EMAIL.partition("@")[2]


def run_email(run: str) -> str:
    """The eval person of run *run*, e.g. ``eval+20261101-093000-ab12@superchat.local``."""
    if not _RUN_PART.match(run):
        raise ValueError(f"not a run id: {run}")
    return f"eval+{run.lower()}@{eval_domain()}"


def run_of(user_id: str | None) -> str | None:
    """The run id a run's eval person was made for, or ``None`` for anyone else."""
    local, at, domain = (user_id or "").lower().rpartition("@")
    if not at or domain != eval_domain() or not local.startswith("eval+"):
        return None
    run = local.removeprefix("eval+")
    return run if _RUN_PART.match(run) else None


def is_eval_identity(user_id: str | None) -> bool:
    """Whether *user_id* is an eval person: a run's, or the shared one. People-facing features
    (people lists, the Lookout, the daily brief, proactive messages, background passes) skip
    them."""
    if not user_id:
        return False
    return run_of(user_id) is not None or user_id.lower() == base_email().lower()


def runner_env(owner: str | None) -> dict[str, str]:
    """What an eval person's host runners get on top of the deployment's ``runner_env``: the
    model-call context records, on for them whatever the trace settings (header values are
    never recorded)."""
    return {CONTEXT_DIR_ENV: CONTEXT_PATH} if is_eval_identity(owner) else {}
