from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import canonical_json
from .store import Database


class MockIM:
    """A durable mock adapter with EffectId idempotency."""

    def __init__(self, database: Database):
        self.database = database

    def send(self, command: dict[str, Any], *, accepted_sim_time: str) -> dict[str, Any]:
        with self.database.transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM mock_im_messages WHERE effect_id = ?",
                (command["effect_id"],),
            ).fetchone()
            if existing:
                return {
                    "external_message_id": existing["external_message_id"],
                    "deduplicated": True,
                    "accepted_sim_time": existing["accepted_sim_time"],
                }

            row = cursor.execute(
                "SELECT COALESCE(MAX(accepted_sequence), 0) + 1 AS next_sequence "
                "FROM mock_im_messages"
            ).fetchone()
            sequence = int(row["next_sequence"])
            external_message_id = f"msg_{uuid4().hex}"
            cursor.execute(
                """
                INSERT INTO mock_im_messages(
                    external_message_id, effect_id, conversation_id,
                    sender_actor_id, recipient_actor_ids, effect_type, content,
                    accepted_sequence, accepted_sim_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    external_message_id,
                    command["effect_id"],
                    command["conversation_id"],
                    command["sender_actor_id"],
                    canonical_json(command["recipient_actor_ids"]),
                    command["effect_type"],
                    command["content"],
                    sequence,
                    accepted_sim_time,
                ),
            )
            return {
                "external_message_id": external_message_id,
                "deduplicated": False,
                "accepted_sim_time": accepted_sim_time,
            }

    def messages(self) -> list[dict[str, Any]]:
        rows = self.database.all(
            "SELECT * FROM mock_im_messages ORDER BY accepted_sequence"
        )
        return [dict(row) for row in rows]

