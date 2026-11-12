"""Tests for conversation entity helpers."""

from __future__ import annotations

from omnigent.entities.conversation import (
    ConversationItem,
    MessageData,
    synthesize_conversation_title,
)


def _message_item(created_by: str | None) -> ConversationItem:
    """Build a persisted user-message item with the given author."""
    return ConversationItem(
        id="msg_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=0,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hi"}],
        ),
        created_by=created_by,
    )


def test_to_api_dict_exposes_created_by_when_set() -> None:
    """A human-authored item surfaces ``created_by`` in the API shape."""
    api = _message_item("alice@example.com").to_api_dict()
    assert api["created_by"] == "alice@example.com"


def test_to_api_dict_omits_created_by_when_none() -> None:
    """Agent/system items omit ``created_by`` so they stay distinguishable."""
    api = _message_item(None).to_api_dict()
    assert "created_by" not in api


def test_synthesize_conversation_title_respects_custom_limit() -> None:
    """Custom ``limit`` is honored."""
    content = [{"type": "input_text", "text": "a" * 50}]
    assert synthesize_conversation_title(content, limit=10) == "a" * 9 + "…"


def test_is_system_notice_by_flag_or_legacy_prefix() -> None:
    from omnigent.entities.conversation import MessageData, is_system_notice

    def user(text: str, **kwargs: bool) -> MessageData:
        return MessageData(role="user", content=[{"type": "input_text", "text": text}], **kwargs)

    assert is_system_notice(user("Researcher finished", is_system_notice=True))
    # Stored before the flag existed: known by its text.
    assert is_system_notice(user("[System: timer 1 fired]"))
    assert not is_system_notice(user("please summarise this"))
    assert "is_system_notice" not in user("hi").model_dump()
    assert user("hi", is_system_notice=True).model_dump()["is_system_notice"] is True
