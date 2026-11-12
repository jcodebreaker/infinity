"""``/v1/artifacts/{id}/publish`` and ``/v1/published/{slug}``: a saved HTML file as a web app.

Publishing is a state on top of an artifact (the publication rows live in the artifacts store),
so this router resolves the artifact through the artifacts capability and adds the app routes.
``/v1/published/*`` is gateway-only (kept for an external gateway); the engine also serves the
app itself at ``GET /apps/<slug>`` (:func:`create_published_pages_router`), checking the viewer
against the audience and locking the page into an opaque origin with its response headers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html as _html
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.superchat.artifacts import (
    AUDIENCES,
    Artifact,
    SqlAlchemyArtifactStore,
    load_artifact,
    publication_to_response,
    request_owner,
)

#: The identity an external ``/apps/<slug>`` gateway calls ``/v1/published`` with. Only this
#: caller may read published bytes or record views: it checks the viewer's audience first.
PUBLISHED_GATEWAY_USER_DEFAULT = "superchat-apps-gateway@superchat.invalid"


def published_gateway_user() -> str:
    """:returns: The configured gateway identity (``OMNIGENT_PUBLISHED_GATEWAY_USER``)."""
    return os.environ.get("OMNIGENT_PUBLISHED_GATEWAY_USER") or PUBLISHED_GATEWAY_USER_DEFAULT


class PublishBody(BaseModel):
    """``POST /artifacts/{id}/publish`` body."""

    audience: str = Field(max_length=8)
    version: int | None = Field(default=None, ge=1)


class ViewBody(BaseModel):
    """``POST /published/{slug}/view`` body."""

    viewer_key: str = Field(min_length=1, max_length=64)


def create_apps_router(
    store: SqlAlchemyArtifactStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the apps router, mounted with ``prefix="/v1"`` after the artifacts router."""
    router = APIRouter()

    async def _load(request: Request, artifact_id: str) -> Artifact:
        return await load_artifact(store, request_owner(request, auth_provider), artifact_id)

    async def _publish_state(item: Artifact) -> dict[str, Any]:
        pub = await asyncio.to_thread(store.publication_for, item)
        if pub is None:
            return {"published": False, "publish": None}
        stats = (await asyncio.to_thread(store.stats, [pub.slug])).get(pub.slug)
        return {"published": True, "publish": publication_to_response(pub, stats)}

    @router.post("/artifacts/{artifact_id}/publish")
    async def publish_artifact(
        request: Request, artifact_id: str, body: PublishBody
    ) -> dict[str, Any]:
        """Publish (or republish) a deliverable as a web app; owner-only, html only.

        Republishing keeps the address and re-pins the served version (default newest).
        """
        item = await _load(request, artifact_id)
        if body.audience not in AUDIENCES:
            raise OmnigentError(
                f"audience must be one of: {', '.join(AUDIENCES)}", code=ErrorCode.INVALID_INPUT
            )
        if item.kind != "html":
            raise OmnigentError("only html files can be published", code=ErrorCode.INVALID_INPUT)
        try:
            pub = await asyncio.to_thread(
                store.publish, item, audience=body.audience, version=body.version
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        stats = (await asyncio.to_thread(store.stats, [pub.slug])).get(pub.slug)
        return publication_to_response(pub, stats)

    @router.delete("/artifacts/{artifact_id}/publish")
    async def unpublish_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """Take the app offline (its address stops working); owner-only."""
        item = await _load(request, artifact_id)
        removed = await asyncio.to_thread(store.unpublish, item)
        return {"unpublished": removed}

    @router.get("/artifacts/{artifact_id}/publish")
    async def get_publish_state(request: Request, artifact_id: str) -> dict[str, Any]:
        """The publish state plus view stats; owner-only."""
        return await _publish_state(await _load(request, artifact_id))

    def _require_gateway(request: Request) -> None:
        if auth_provider is None:
            return
        caller = require_user(request, auth_provider)
        if caller != published_gateway_user():
            raise OmnigentError("Not found", code=ErrorCode.NOT_FOUND)

    @router.get("/published/{slug}")
    async def read_published(request: Request, slug: str) -> Response:
        """The pinned HTML bytes of a published app plus its audience and owner.

        Gateway-only: the gateway checks the viewer against the audience first. The viewer-facing
        security headers are the gateway's job; this response is never served to a browser.
        """
        _require_gateway(request)
        found = await asyncio.to_thread(store.published_by_slug, slug)
        if found is None:
            raise OmnigentError("Not found", code=ErrorCode.NOT_FOUND)
        pub, item = found
        data = await asyncio.to_thread(store.read, item)
        from omnigent.db.db_models import current_workspace_id

        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "X-Published-Audience": pub.audience,
                "X-Published-Owner": quote(pub.user_id or ""),
                "X-Published-Workspace": str(current_workspace_id()),
                "X-Published-Version": str(pub.published_version),
                "X-Published-Title": quote((item.title or item.name)[:200]),
                "Cache-Control": "no-store",
            },
        )

    @router.post("/published/{slug}/view")
    async def record_published_view(request: Request, slug: str, body: ViewBody) -> dict[str, Any]:
        """Count one open by an opaque viewer key (gateway-only)."""
        _require_gateway(request)
        counted = await asyncio.to_thread(store.record_view, slug, body.viewer_key)
        return {"counted": counted}

    return router


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,94}$")

#: Opaque origin, no network, no engine cookies: scripts and inline styles run, nothing leaves.
PUBLISHED_APP_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals; default-src 'none'; "
    "script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; "
    "img-src data: blob: https:; font-src data: https:; media-src data: blob: https:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def published_app_headers(audience: str | None) -> dict[str, str]:
    """Headers every ``/apps/<slug>`` response carries, whatever its status."""
    return {
        "content-security-policy": PUBLISHED_APP_CSP,
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "cache-control": "public, no-cache" if audience == "link" else "no-store",
    }


def anonymous_viewer_key(secret: bytes, ip: str, user_agent: str, day: str) -> str:
    """Same-day, same-network viewers collapse to one key; it changes every UTC day."""
    salt = hmac.new(secret, f"published-apps:{day}".encode(), hashlib.sha256).digest()
    digest = hmac.new(salt, f"{ip}\n{user_agent}".encode(), hashlib.sha256).hexdigest()
    return f"a:{digest[:32]}"


def _notice_page(title: str, body: str) -> str:
    t, b = _html.escape(title), _html.escape(body)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{t}</title><style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;"
        "min-height:100vh;display:grid;place-items:center;background:#faf9f7;color:#1c1b19}"
        "main{padding:24px;max-width:28rem;text-align:center}"
        "h1{font-size:1.25rem;margin:0 0 .5rem}"
        f"p{{margin:.25rem 0;color:#5a5852}}</style></head><body><main><h1>{t}</h1><p>{b}</p>"
        "</main></body></html>"
    )


def create_published_pages_router(
    store: SqlAlchemyArtifactStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the ``GET /apps/{slug}`` router, mounted at the origin root (no prefix)."""
    router = APIRouter()
    view_secret = (os.environ.get("OMNIGENT_VAULT_KEY") or secrets.token_hex(32)).encode()

    def _viewer(request: Request) -> str | None:
        if auth_provider is None:
            return RESERVED_USER_LOCAL
        try:
            return auth_provider.get_user_id(request)
        except OmnigentError:
            return None

    def _reply(status: int, body: str | bytes, audience: str | None, method: str) -> Response:
        return Response(
            content=b"" if method == "HEAD" else body,
            status_code=status,
            headers={
                **published_app_headers(audience if status == 200 else None),
                "content-type": "text/html; charset=utf-8",
                "content-disposition": "inline",
            },
        )

    @router.api_route("/apps/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_published_app(request: Request, slug: str) -> Response:
        """The pinned HTML of a published app, if the viewer is within its audience."""
        method = request.method
        not_found = _notice_page("Not found", "This app does not exist or is no longer published.")
        if not _SLUG.match(slug):
            return _reply(404, not_found, None, method)
        found = await asyncio.to_thread(store.published_by_slug, slug)
        if found is None:
            return _reply(404, not_found, None, method)
        pub, item = found
        owner = pub.user_id or RESERVED_USER_LOCAL
        viewer = _viewer(request)
        is_owner = viewer is not None and viewer == owner
        allowed = (
            pub.audience == "link"
            # The engine is one workspace, so "org" is any signed-in person in it.
            or (pub.audience == "org" and viewer is not None)
            or is_owner
        )
        if not allowed:
            title, body = (
                ("Not available to you", "The owner has not shared this app with you.")
                if viewer
                else ("Private app", "Sign in to see whether you can open this app.")
            )
            return _reply(403, _notice_page(title, body), None, method)
        data = await asyncio.to_thread(store.read, item)
        if method == "GET" and not is_owner:
            if viewer is not None:
                key = f"u:{viewer}"
            else:
                client = request.client.host if request.client else "unknown"
                ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or client
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = anonymous_viewer_key(
                    view_secret, ip, request.headers.get("user-agent", ""), day
                )
            # A failed view count never blocks the page.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.record_view, slug, key)
        return _reply(200, data, pub.audience, method)

    return router
