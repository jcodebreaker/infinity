"""The closed set of failure codes a client may rely on, and where they come from.

ADR 0009: the engine returns stable codes, wording stays with clients. A turn that fails at
the provider (or in the harness around it) is stored as an ``error`` item carrying one of
:data:`PUBLIC_ERROR_CODES`. A raw exception class name (``RuntimeError``) is never a public
code: it is :data:`INTERNAL`.

Two entry points:

* :func:`classify_provider_failure` -- at the point a failure is created. Structured data
  first (HTTP status, the provider's error type); matching the message text is the last
  resort, a provider translation that lives only in the harness adapter that calls it.
* :func:`public_error_code` -- at read time. Maps any code already stored (old rows,
  internal retry codes, exception names) into the closed set. Specific operational codes
  with no public meaning (``native_terminal_start_failed`` ...) map to :data:`INTERNAL`.
"""

from __future__ import annotations

import re

INSUFFICIENT_CREDIT = "insufficient_credit"  # the account behind the model has no credit/quota
RATE_LIMITED = "rate_limited"  # the provider throttled the request; retry shortly
OVERLOADED = "overloaded"  # the provider is at capacity (Anthropic 529)
AUTH_FAILED = "auth_failed"  # the provider rejected the credentials (401/403)
CONTEXT_TOO_LONG = "context_too_long"  # the conversation no longer fits the model's window
PROVIDER_UNAVAILABLE = "provider_unavailable"  # provider 5xx, unreachable, or dropped connection
TIMEOUT = "timeout"  # the turn or the provider call took too long
SANDBOX_UNAVAILABLE = "sandbox_unavailable"  # the host/sandbox the turn runs in is gone
# Not a failure (stored at ``level: info``): the sandbox was replaced by a fresh one and files
# it held that were not saved elsewhere are gone. The chat goes on.
WORKSPACE_RESET = "workspace_reset"
MODEL_KEY_REQUIRED = "model_key_required"  # the owner has not saved a model API key (Settings)
# The chosen model is not served by the owner's connection (e.g. a Claude-only key, a GPT model).
MODEL_NOT_SUPPORTED = "model_not_supported"
# The owner's (or their organization's) monthly model budget is used up and set to stop.
MODEL_BUDGET_EXHAUSTED = "model_budget_exhausted"
# An admin suspended the account behind the request; only an admin can lift it.
ACCOUNT_SUSPENDED = "account_suspended"
# The reply was shown live but could not be saved (e.g. the database is full): it is missing
# from the history after a reload.
REPLY_NOT_SAVED = "reply_not_saved"
INTERNAL = "internal"  # anything else: our bug or an unclassified failure

PUBLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        INSUFFICIENT_CREDIT,
        RATE_LIMITED,
        OVERLOADED,
        AUTH_FAILED,
        CONTEXT_TOO_LONG,
        PROVIDER_UNAVAILABLE,
        TIMEOUT,
        SANDBOX_UNAVAILABLE,
        WORKSPACE_RESET,
        MODEL_KEY_REQUIRED,
        MODEL_NOT_SUPPORTED,
        MODEL_BUDGET_EXHAUSTED,
        ACCOUNT_SUSPENDED,
        REPLY_NOT_SAVED,
        INTERNAL,
    }
)

# Provider error ``type`` / SDK error enum (Anthropic API and ``claude_agent_sdk``) -> code.
_BY_ERROR_TYPE: dict[str, str] = {
    "billing_error": INSUFFICIENT_CREDIT,
    "rate_limit_error": RATE_LIMITED,
    "rate_limit": RATE_LIMITED,
    "overloaded_error": OVERLOADED,
    "authentication_error": AUTH_FAILED,
    "authentication_failed": AUTH_FAILED,
    "permission_error": AUTH_FAILED,
    "api_error": PROVIDER_UNAVAILABLE,
    "server_error": PROVIDER_UNAVAILABLE,
    "timeout_error": TIMEOUT,
    "model_budget_exhausted": MODEL_BUDGET_EXHAUSTED,
    # The error type wins over the status, so the proxy's 403 reads as a suspension, not as a
    # rejected key (``auth_failed``).
    "account_suspended": ACCOUNT_SUSPENDED,
}

# HTTP status -> code. 402 is "payment required"; 413 is the API's request-too-large.
_BY_STATUS: dict[int, str] = {
    401: AUTH_FAILED,
    402: INSUFFICIENT_CREDIT,
    403: AUTH_FAILED,
    408: TIMEOUT,
    413: CONTEXT_TOO_LONG,
    429: RATE_LIMITED,
    504: TIMEOUT,
    529: OVERLOADED,
}

# Provider translation, last resort: harnesses whose failure arrives only as text.
_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), code)
    for pattern, code in (
        (
            r"credit balance is too low|insufficient[_ ](credit|quota|funds)|billing",
            INSUFFICIENT_CREDIT,
        ),
        (
            r"prompt is too long|context[_ ](length|window)|maximum context length",
            CONTEXT_TOO_LONG,
        ),
        (r"rate[_ ]limit|too many requests|\b429\b", RATE_LIMITED),
        (r"overloaded|\b529\b", OVERLOADED),
        (r"invalid api key|authentication|unauthori[sz]ed|\b40[13]\b", AUTH_FAILED),
        (r"timed? ?out|\b504\b", TIMEOUT),
        (
            r"\b50[023]\b|service unavailable|bad gateway|connection (error|refused|reset)",
            PROVIDER_UNAVAILABLE,
        ),
    )
)


def classify_provider_failure(
    *,
    error_type: str | None = None,
    status: int | None = None,
    message: str | None = None,
) -> str | None:
    """Classify a provider failure into a public code, or ``None`` when it cannot be told.

    :param error_type: The provider's own error type, e.g. ``"billing_error"``.
    :param status: HTTP status of the failing call, e.g. ``429``.
    :param message: The failure text; consulted only when nothing structured matched.
    :returns: A member of :data:`PUBLIC_ERROR_CODES` (never :data:`INTERNAL`), or ``None``.
    """
    if error_type and error_type in _BY_ERROR_TYPE:
        return _BY_ERROR_TYPE[error_type]
    if status is not None:
        if status in _BY_STATUS:
            return _BY_STATUS[status]
        if 500 <= status <= 599:
            return PROVIDER_UNAVAILABLE
    if message:
        for pattern, code in _TEXT_RULES:
            if pattern.search(message):
                return code
    return None


# Codes the engine used before the closed set (internal retry codes and stored rows).
_LEGACY: dict[str, str] = {
    "rate_limit_exceeded": RATE_LIMITED,
    "connection_error": PROVIDER_UNAVAILABLE,
    "server_error": PROVIDER_UNAVAILABLE,
    "context_length_exceeded": CONTEXT_TOO_LONG,
    "budget_exhausted": INSUFFICIENT_CREDIT,
    "runner_disconnected": SANDBOX_UNAVAILABLE,
    "runner_unavailable": SANDBOX_UNAVAILABLE,
    "managed_sandbox_workspace_reset": WORKSPACE_RESET,
    **_BY_ERROR_TYPE,
}


def public_error_code(code: str | None) -> str:
    """Map any stored or internal failure code into :data:`PUBLIC_ERROR_CODES`.

    :param code: A code as stored, e.g. ``"RuntimeError"`` or ``"rate_limit_exceeded"``.
    :returns: The public code; unknown codes, exception class names included, are :data:`INTERNAL`.
    """
    if not code:
        return INTERNAL
    if code in PUBLIC_ERROR_CODES:
        return code
    return _LEGACY.get(code, INTERNAL)
