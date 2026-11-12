"""Built-in tools for the secrets vault. Schema-only: the runner dispatches them.

* ``vault_request_secret`` — ask the person to save a login on a secure form; the value
  never enters the chat. Returns a request id the client renders as a secure-entry card.
* ``vault_fill`` — type one saved value into a field of the host's browser. The result only
  says "filled"; the model never sees the value.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

VAULT_TOOL_NAMES = ("vault_request_secret", "vault_fill")
# The only vault tool a Helper (sub-agent session) may call: it cannot talk to the person.
VAULT_HELPER_TOOL_NAMES = ("vault_fill",)


class _VaultTool(Tool):
    _NAME = ""
    _DESC = ""
    _PROPERTIES: dict[str, Any] = {}
    _REQUIRED: tuple[str, ...] = ()

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return cls._NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return cls._DESC

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self._PROPERTIES,
                    "required": list(self._REQUIRED),
                    "additionalProperties": False,
                },
            },
        }


class VaultRequestSecretTool(_VaultTool):
    """Open a secure-entry request; dispatched to ``POST /v1/me/vault/requests``."""

    _NAME = "vault_request_secret"
    _DESC = (
        "Ask the person to save a website login in the vault. They enter it on a secure card, "
        "never in chat. Then use vault_fill. Never ask for a password in chat."
    )
    _PROPERTIES = {
        "name": {"type": "string", "description": "Short reference name, e.g. acme_login."},
        "site": {"type": "string", "description": "The https address of the sign-in page."},
        "reason": {"type": "string", "description": "One short line shown on the card."},
    }
    _REQUIRED = ("name", "site")


class VaultFillTool(_VaultTool):
    """Fill a field from the vault; handled in the runner against the host's browser."""

    _NAME = "vault_fill"
    _DESC = (
        "Fill a login field in the host's browser from a saved login. Find the field's ref "
        "with browser_snapshot. Works only on the site the login was saved for. Codes (2FA, "
        "CAPTCHA) cannot be filled: tell the person and stop."
    )
    _PROPERTIES = {
        "name": {"type": "string", "description": "The saved login's name."},
        "field": {"type": "string", "enum": ["username", "password"]},
        "ref": {"type": "integer", "description": "Field ref from browser_snapshot."},
    }
    _REQUIRED = ("name", "field", "ref")
