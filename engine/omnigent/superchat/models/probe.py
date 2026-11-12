"""Validate a provider API key with one cheap authenticated GET; the key is never logged."""

from __future__ import annotations

import httpx

from omnigent.superchat.models.upstreams import PROVIDERS, UPSTREAMS

__all__ = ["PROVIDERS", "KeyRejectedError", "ProbeUnavailableError", "probe_key", "provider_label"]

_TIMEOUT_SECONDS = 10.0


class KeyRejectedError(Exception):
    """The provider answered 401/403: the key is not valid."""


class ProbeUnavailableError(Exception):
    """The provider could not be reached or answered something unexpected."""


def provider_label(provider: str) -> str:
    """:returns: The display name of provider."""
    return UPSTREAMS[provider].label


async def probe_key(
    provider: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Return when the provider accepts the key.

    :param transport: Injectable httpx transport (tests are offline).
    :raises KeyRejectedError: 401/403.
    :raises ProbeUnavailableError: network error, timeout or any other status.
    """
    upstream = UPSTREAMS[provider]
    label = upstream.label
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(upstream.probe_url, headers=upstream.auth_headers(api_key))
    except httpx.HTTPError as exc:
        # Only the exception type is kept: its text could carry request details.
        raise ProbeUnavailableError(
            f"Could not reach {label} to check the key ({type(exc).__name__}); try again"
        ) from None
    if response.status_code in (401, 403):
        raise KeyRejectedError(f"This key was not accepted by {label}")
    if response.status_code != 200:
        raise ProbeUnavailableError(
            f"{label} could not check the key right now (HTTP {response.status_code}); try again"
        )
