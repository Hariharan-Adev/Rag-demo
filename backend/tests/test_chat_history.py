"""Backend-persisted chat history tests."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.services.chat_history import (
    append_exchange,
    delete_conversation,
    list_conversations,
    update_conversation,
)


class ChatHistoryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "history.db"
        self.patch = patch.object(database, "DATABASE_PATH", self.database_path)
        self.patch.start()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'hash')",
                [(1, "owner@example.com"), (2, "other@example.com")],
            )

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def test_chat_exchange_is_listed_for_same_owner_only(self) -> None:
        append_exchange(
            owner_id=1,
            conversation_id="shared-port-chat",
            question="compare skills",
            answer="Grounded answer",
            sources=[{"document_id": 10, "filename": "matrix.xlsx"}],
        )

        conversations = list_conversations(1)
        other_owner = list_conversations(2)

        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["id"], "shared-port-chat")
        self.assertEqual([message["role"] for message in conversations[0]["messages"]], ["user", "assistant"])
        self.assertEqual(conversations[0]["messages"][1]["content"], "Grounded answer")
        self.assertEqual(other_owner, [])

    def test_metadata_updates_and_soft_delete_are_owner_scoped(self) -> None:
        append_exchange(
            owner_id=1,
            conversation_id="owned-chat",
            question="hello",
            answer="answer",
            sources=[],
        )

        self.assertFalse(update_conversation(owner_id=2, conversation_id="owned-chat", title="Wrong"))
        self.assertTrue(update_conversation(owner_id=1, conversation_id="owned-chat", title="Renamed", is_pinned=True))
        updated = list_conversations(1)[0]

        self.assertEqual(updated["title"], "Renamed")
        self.assertTrue(updated["is_pinned"])
        self.assertFalse(delete_conversation(2, "owned-chat"))
        self.assertTrue(delete_conversation(1, "owned-chat"))
        self.assertEqual(list_conversations(1), [])


if __name__ == "__main__":
    unittest.main()
