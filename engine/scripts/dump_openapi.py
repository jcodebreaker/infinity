"""Generate ``openapi.json`` from the engine's FastAPI app.

Builds the app against throwaway SQLite stores (``app.openapi()`` walks routes and
models only), then post-processes FastAPI's OpenAPI 3.1 output:

1. Bumps ``openapi`` to 3.2.0 so SSE routes can use the ``itemSchema`` keyword.
2. Adds the ``ServerStreamEvent`` union and its variants to ``components.schemas``.
3. Rewrites the SSE routes' ``text/event-stream`` ``schema`` to ``itemSchema``.
4. Stamps ``int64`` / ``double`` on formatless numeric component schemas, so
   generated clients do not narrow them to int32 / float32.

``tests/server/test_openapi_drift.py`` compares the committed file with
:func:`generate_spec` byte for byte.

Usage::

    uv run python scripts/dump_openapi.py          # write openapi.json
    uv run python scripts/dump_openapi.py --check  # exit 1 if it drifted
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_ENGINE_ROOT: Path = Path(__file__).resolve().parent.parent
_OPENAPI_OUT: Path = _ENGINE_ROOT / "openapi.json"
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

_OPENAPI_VERSION: str = "3.2.0"

# Routes that stream Server-Sent Events, keyed as in the ``paths`` map.
_SSE_ROUTES: list[tuple[str, str]] = [
    ("/v1/sessions/{session_id}/stream", "get"),
]

# Keys whose values are payloads, not schemas; numbers under them are data.
_SCHEMA_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"default", "example", "examples", "const", "enum"},
)


def _build_app() -> Any:
    """
    Build the server app with on-disk SQLite stores in a temp directory.

    Passes the stores ``deployment/entrypoint.py`` passes, so the routes they gate are in the
    spec. Memory is left out: its service needs an optional extra, and the spec must not
    depend on what is installed.

    :returns: The configured FastAPI app.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.objective_store.sqlalchemy_store import SqlAlchemyObjectiveStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    workdir = Path(tempfile.mkdtemp(prefix="engine-openapi-"))
    db_uri = f"sqlite:///{workdir / 'spec.sqlite'}"
    artifact_store = LocalArtifactStore(str(workdir / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        comment_store=SqlAlchemyCommentStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=workdir / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
        host_store=HostStore(db_uri),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        objective_store=SqlAlchemyObjectiveStore(db_uri),
        # Header auth, the deployment default; every value explicit so no env var leaks in.
        auth_provider=UnifiedAuthProvider(
            source="header",
            local_single_user=False,
            header_name="X-Forwarded-Email",
            header_strip_prefix="",
            header_secret="",
            tenant_header="",
        ),
    )


def _pin_multi_method_operation_ids(app: Any) -> None:
    """
    Make FastAPI's operation id for a multi-method route independent of hash order.

    FastAPI names such a route after ``list(route.methods)[0]``, a set whose order
    changes with ``PYTHONHASHSEED``, so the same app would dump different specs.
    Use the alphabetically first method instead.

    :param app: The FastAPI app; its routes are mutated in place.
    """
    from fastapi.routing import APIRoute
    from fastapi.utils import generate_unique_id

    for route in app.routes:
        if isinstance(route, APIRoute) and not route.operation_id and len(route.methods) > 1:
            first = min(route.methods)
            route.unique_id = generate_unique_id(route).rsplit("_", 1)[0] + f"_{first.lower()}"


def _add_stream_event_schemas(schemas: dict[str, Any]) -> None:
    """
    Add the ``ServerStreamEvent`` union and its variants to *schemas*.

    A variant FastAPI already emitted (e.g. ``ResponseObject``) is kept as is.

    :param schemas: ``components.schemas``; mutated in place.
    """
    from pydantic import TypeAdapter

    from omnigent.server.schemas import ServerStreamEvent

    union = TypeAdapter(ServerStreamEvent).json_schema(
        ref_template="#/components/schemas/{model}",
    )
    for name, definition in union.pop("$defs", {}).items():
        schemas.setdefault(name, definition)
    schemas["ServerStreamEvent"] = union


def _rewrite_sse_routes(paths: dict[str, Any]) -> None:
    """
    Describe each SSE route's events with the OAS 3.2 ``itemSchema`` keyword.

    :param paths: The ``paths`` map; mutated in place.
    """
    for path, method in _SSE_ROUTES:
        op = paths.get(path, {}).get(method)
        if op is None:
            continue
        sse = op.get("responses", {}).get("200", {}).get("content", {}).get("text/event-stream")
        if sse is not None and "schema" in sse:
            sse["itemSchema"] = sse.pop("schema")


def _annotate_number_formats(components: dict[str, Any]) -> None:
    """
    Stamp ``double`` / ``int64`` on formatless numeric component schemas.

    Uses ``setdefault`` so a format the model sets itself wins. Payload values
    (``default``, ``example``, ``enum``, ...) are not schemas and are skipped;
    a property merely *named* ``default`` is still a schema.

    :param components: ``components``; its ``schemas`` are mutated in place.
    """

    def walk(node: Any, *, keys_are_names: bool = False) -> None:
        if isinstance(node, dict):
            if not keys_are_names:
                if node.get("type") == "number":
                    node.setdefault("format", "double")
                elif node.get("type") == "integer":
                    node.setdefault("format", "int64")
            for key, value in node.items():
                if keys_are_names:
                    walk(value)
                elif key in ("properties", "patternProperties"):
                    walk(value, keys_are_names=True)
                elif key not in _SCHEMA_PAYLOAD_KEYS:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(components.get("schemas", {}), keys_are_names=True)


def generate_spec() -> dict[str, Any]:
    """
    Generate the post-processed OpenAPI document.

    :returns: The spec dict, ready to serialize.
    """
    app = _build_app()
    _pin_multi_method_operation_ids(app)
    spec: dict[str, Any] = app.openapi()
    spec["openapi"] = _OPENAPI_VERSION
    components = spec.setdefault("components", {})
    _add_stream_event_schemas(components.setdefault("schemas", {}))
    _rewrite_sse_routes(spec.get("paths", {}))
    _annotate_number_formats(components)
    return spec


def main() -> int:
    """
    Write ``openapi.json``, or with ``--check`` report whether it drifted.

    :returns: 0 on success, 1 when ``--check`` finds drift.
    """
    parser = argparse.ArgumentParser(description="Generate engine/openapi.json.")
    parser.add_argument("--check", action="store_true", help="exit 1 if openapi.json drifted")
    args = parser.parse_args()

    serialized = json.dumps(generate_spec(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not _OPENAPI_OUT.exists() or _OPENAPI_OUT.read_text() != serialized:
            sys.stderr.write("openapi.json is out of date; run scripts/dump_openapi.py.\n")
            return 1
        return 0
    _OPENAPI_OUT.write_text(serialized)
    sys.stdout.write(f"Wrote {_OPENAPI_OUT}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
