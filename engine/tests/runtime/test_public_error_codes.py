"""Failure classification into the closed public code set."""

from __future__ import annotations

import pytest

from omnigent.runtime.public_error_codes import (
    INTERNAL,
    PUBLIC_ERROR_CODES,
    classify_provider_failure,
    public_error_code,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"error_type": "billing_error"}, "insufficient_credit"),
        ({"error_type": "rate_limit_error"}, "rate_limited"),
        ({"error_type": "rate_limit"}, "rate_limited"),
        ({"error_type": "overloaded_error"}, "overloaded"),
        ({"error_type": "authentication_error"}, "auth_failed"),
        ({"error_type": "authentication_failed"}, "auth_failed"),
        ({"status": 401}, "auth_failed"),
        ({"status": 402}, "insufficient_credit"),
        ({"status": 429}, "rate_limited"),
        ({"status": 529}, "overloaded"),
        ({"status": 504}, "timeout"),
        ({"status": 500}, "provider_unavailable"),
        ({"status": 503}, "provider_unavailable"),
        # Structured data beats the text.
        ({"error_type": "rate_limit", "message": "Credit balance is too low"}, "rate_limited"),
        ({"status": 429, "message": "overloaded"}, "rate_limited"),
    ],
)
def test_structured_data_classifies(kwargs: dict[str, object], expected: str) -> None:
    assert classify_provider_failure(**kwargs) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Credit balance is too low", "insufficient_credit"),
        ("Prompt is too long", "context_too_long"),
        ("429 rate limit reached", "rate_limited"),
        ("API Error: Overloaded", "overloaded"),
        ("Invalid API key", "auth_failed"),
        ("request timed out", "timeout"),
        ("502 Bad Gateway", "provider_unavailable"),
    ],
)
def test_text_is_the_last_resort(message: str, expected: str) -> None:
    assert classify_provider_failure(message=message) == expected


def test_unclassifiable_is_none() -> None:
    assert classify_provider_failure(message="something odd", status=400) is None
    assert classify_provider_failure() is None


def test_public_error_code_closed_set() -> None:
    for code in PUBLIC_ERROR_CODES:
        assert public_error_code(code) == code
    assert public_error_code("RuntimeError") == INTERNAL
    assert public_error_code("native_terminal_start_failed") == INTERNAL
    assert public_error_code(None) == INTERNAL
    assert public_error_code("rate_limit_exceeded") == "rate_limited"
