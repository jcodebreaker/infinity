"""The ``card_answers`` table: the picks on the cards that asked. The ``tasks`` table of ADR 0025
is no longer read or written (the Round record took its place, ADR 0029); its old rows go with
their Conversation."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlCardAnswer, SqlTask, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.tasks.record import CardAnswer


class SqlAlchemyTaskStore:
    """SQLAlchemy-backed store for the ``card_answers`` table (and the old ``tasks`` rows)."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.task_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.task_store", immediate=True
        )

    def save_answer(self, conversation_id: str, item_id: str, answer: CardAnswer) -> None:
        """Keep *answer* on the person's message *item_id* (once; a repeat replaces it)."""

        def write(session: Session) -> None:
            row = session.get(SqlCardAnswer, (current_workspace_id(), conversation_id, item_id))
            if row is None:
                row = SqlCardAnswer(session_id=conversation_id, item_id=item_id)
                session.add(row)
            row.card_id = answer.card_id
            row.option_id = answer.option_id
            row.label = answer.label
            row.created_at = now_epoch()

        run_write_transaction(self._session_immediate, "card_answer_save", write)

    def answer(self, conversation_id: str, item_id: str) -> CardAnswer | None:
        """The card answer the person's message *item_id* carried, or ``None``."""
        with self._session("card_answer_get") as session:
            row = session.get(SqlCardAnswer, (current_workspace_id(), conversation_id, item_id))
            return CardAnswer(row.card_id, row.option_id, row.label) if row is not None else None

    def delete_conversation(self, conversation_id: str) -> None:
        """Drop every task and card answer of a deleted Conversation."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlTask).where(
                    SqlTask.workspace_id == current_workspace_id(),
                    SqlTask.conversation_id == conversation_id,
                )
            )
            session.execute(
                delete(SqlCardAnswer).where(
                    SqlCardAnswer.workspace_id == current_workspace_id(),
                    SqlCardAnswer.session_id == conversation_id,
                )
            )

        run_write_transaction(self._session_immediate, "task_delete_conversation", write)
