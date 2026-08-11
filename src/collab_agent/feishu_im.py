from __future__ import annotations

from typing import Any, Callable, Protocol
from uuid import uuid4

from .feishu_cards import render_command
from .feishu_config import FeishuConfig
from .models import canonical_json


# Kept as separate statements rather than one script so the same DDL runs on
# both the SQLite cursor and the PostgreSQL cursor, which has no executescript.
FEISHU_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS feishu_identity_bindings (
        actor_id TEXT PRIMARY KEY,
        open_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        bound_sim_time TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feishu_im_messages (
        external_message_id TEXT PRIMARY KEY,
        effect_id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL,
        sender_actor_id TEXT NOT NULL,
        recipient_actor_ids TEXT NOT NULL,
        receive_id TEXT NOT NULL,
        receive_id_type TEXT NOT NULL,
        effect_type TEXT NOT NULL,
        content TEXT NOT NULL,
        accepted_sequence INTEGER NOT NULL UNIQUE,
        accepted_sim_time TEXT NOT NULL,
        provider_deduplicated INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feishu_inbound_actions (
        action_id TEXT PRIMARY KEY,
        event_key TEXT NOT NULL UNIQUE,
        operator_open_id TEXT NOT NULL,
        actor_id TEXT,
        action_name TEXT NOT NULL,
        effect_id TEXT,
        raw_value TEXT NOT NULL,
        status TEXT NOT NULL,
        received_sim_time TEXT NOT NULL,
        processed_sim_time TEXT,
        process_error TEXT
    )
    """,
)


class FeishuIdentityUnbound(RuntimeError):
    """Raised when an actor has no Feishu open_id binding.

    Deliberately fatal for the attempt: guessing a recipient would send a task
    to the wrong person, which is worse than leaving the Outbox entry for the
    dispatcher recovery path to retry after the binding exists.
    """


class FeishuSendFailed(RuntimeError):
    """Raised when the Feishu API rejects a send."""


class FeishuTransport(Protocol):
    def send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
        uuid: str,
    ) -> tuple[str, bool]:
        """Send one message and return (message_id, provider_deduplicated)."""


class RecordingTransport:
    """An offline transport that records calls instead of reaching Feishu.

    Used by the tests and by `--dry-run`, so the whole adapter path including
    identity resolution and idempotency can be exercised without a tenant.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._seen: dict[str, str] = {}

    def update_message(self, *, message_id: str, content: str) -> None:
        self.updates.append({"message_id": message_id, "content": content})

    def send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
        uuid: str,
    ) -> tuple[str, bool]:
        self.calls.append(
            {
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "msg_type": msg_type,
                "content": content,
                "uuid": uuid,
            }
        )
        if uuid in self._seen:
            return self._seen[uuid], True
        message_id = f"om_offline_{uuid4().hex[:16]}"
        self._seen[uuid] = message_id
        return message_id, False


class LarkTransport:
    """The real transport, backed by the official lark-oapi SDK.

    `lark_oapi` is imported lazily so the core package keeps running on the
    standard library alone when Feishu is not in play.
    """

    def __init__(self, config: FeishuConfig) -> None:
        import lark_oapi as lark

        self._lark = lark
        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .build()
        )

    def send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
        uuid: str,
    ) -> tuple[str, bool]:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                # The Outbox EffectId doubles as the provider-side idempotency
                # key. If this process dies after Feishu accepted but before the
                # local row is written, the retry sends the same uuid and Feishu
                # returns the original message instead of a duplicate.
                .uuid(uuid)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise FeishuSendFailed(
                f"feishu send failed code={response.code} msg={response.msg} "
                f"log_id={getattr(response, 'get_log_id', lambda: None)()}"
            )
        message_id = getattr(response.data, "message_id", None)
        if not message_id:
            raise FeishuSendFailed("feishu send returned no message_id")
        return message_id, False

    def update_message(self, *, message_id: str, content: str) -> None:
        """Replace a card in place, so a decided one stops offering buttons."""

        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            raise FeishuSendFailed(
                f"feishu card update failed code={response.code} msg={response.msg}"
            )


class FeishuIM:
    """A durable Feishu adapter with the same contract as `MockIM`.

    `send` accepts the exact command shape the Outbox dispatcher already builds
    (`effect_id`, `effect_type`, `conversation_id`, `sender_actor_id`,
    `recipient_actor_ids`, `content`) and returns the same receipt keys, so
    `CoordinationService.dispatch_all` needs no change to talk to a real tenant.
    """

    def __init__(
        self,
        database: Any,
        transport: FeishuTransport,
        *,
        renderer: Callable[[dict[str, Any]], tuple[str, str]] = render_command,
    ) -> None:
        self.database = database
        self.transport = transport
        self.renderer = renderer
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.database.transaction() as cursor:
            for statement in FEISHU_SCHEMA_STATEMENTS:
                cursor.execute(statement)

    # ---- identity -----------------------------------------------------

    def bind_actor(
        self, actor_id: str, open_id: str, *, display_name: str = "", sim_time: str
    ) -> None:
        """Bind one internal actor to one Feishu open_id.

        Binding is explicit for the same reason the meeting roster is explicit:
        the system must never infer who a participant is.
        """

        with self.database.transaction() as cursor:
            existing = cursor.execute(
                "SELECT open_id FROM feishu_identity_bindings WHERE actor_id = ?",
                (actor_id,),
            ).fetchone()
            if existing:
                cursor.execute(
                    "UPDATE feishu_identity_bindings SET open_id = ?, "
                    "display_name = ?, bound_sim_time = ? WHERE actor_id = ?",
                    (open_id, display_name, sim_time, actor_id),
                )
                return
            cursor.execute(
                "INSERT INTO feishu_identity_bindings("
                "actor_id, open_id, display_name, bound_sim_time"
                ") VALUES (?, ?, ?, ?)",
                (actor_id, open_id, display_name, sim_time),
            )

    def unbind_actor(self, actor_id: str) -> bool:
        """Remove one binding. Returns whether anything was removed.

        Needed as much for cleanup as for offboarding: a placeholder binding
        left over from a copy-pasted example would otherwise sit there routing
        real tasks at a non-existent open_id.
        """

        with self.database.transaction() as cursor:
            removed = cursor.execute(
                "DELETE FROM feishu_identity_bindings WHERE actor_id = ?", (actor_id,)
            ).rowcount
        return bool(removed)

    def open_id_for(self, actor_id: str) -> str:
        row = self.database.one(
            "SELECT open_id FROM feishu_identity_bindings WHERE actor_id = ?",
            (actor_id,),
        )
        if not row:
            raise FeishuIdentityUnbound(
                f"actor {actor_id!r} has no Feishu binding; run "
                "`collab-agent feishu-bind` before dispatching to this person"
            )
        return row["open_id"]

    def actor_for_open_id(self, open_id: str) -> str | None:
        row = self.database.one(
            "SELECT actor_id FROM feishu_identity_bindings WHERE open_id = ?",
            (open_id,),
        )
        return row["actor_id"] if row else None

    def bindings(self) -> list[dict[str, Any]]:
        rows = self.database.all(
            "SELECT * FROM feishu_identity_bindings ORDER BY actor_id"
        )
        return [dict(row) for row in rows]

    # ---- outbound -----------------------------------------------------

    def _existing_receipt(self, effect_id: str) -> dict[str, Any] | None:
        row = self.database.one(
            "SELECT * FROM feishu_im_messages WHERE effect_id = ?", (effect_id,)
        )
        return dict(row) if row else None

    def send(
        self, command: dict[str, Any], *, accepted_sim_time: str
    ) -> dict[str, Any]:
        effect_id = command["effect_id"]

        # Fast path: this effect already reached Feishu in an earlier attempt.
        existing = self._existing_receipt(effect_id)
        if existing:
            return {
                "external_message_id": existing["external_message_id"],
                "deduplicated": True,
                "accepted_sim_time": existing["accepted_sim_time"],
            }

        recipients = list(command["recipient_actor_ids"])
        if not recipients:
            raise ValueError(f"effect {effect_id} has no recipient")
        open_ids = [self.open_id_for(actor_id) for actor_id in recipients]

        msg_type, content = self.renderer(command)

        # The network call sits outside any transaction on purpose: holding a
        # write transaction open across a remote call would block the other
        # process for the whole round trip. Crash safety comes from the uuid.
        message_ids: list[str] = []
        provider_deduplicated = False
        for index, open_id in enumerate(open_ids):
            # One Feishu message per recipient, so each needs its own
            # idempotency key derived from the single EffectId.
            send_uuid = effect_id if len(open_ids) == 1 else f"{effect_id}_{index}"
            message_id, deduplicated = self.transport.send_message(
                receive_id=open_id,
                receive_id_type="open_id",
                msg_type=msg_type,
                content=content,
                uuid=send_uuid,
            )
            message_ids.append(message_id)
            provider_deduplicated = provider_deduplicated or deduplicated

        with self.database.transaction() as cursor:
            # Re-check under the write lock: a concurrent dispatcher may have
            # won the race while this one was on the network.
            raced = cursor.execute(
                "SELECT * FROM feishu_im_messages WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if raced:
                raced = dict(raced)
                return {
                    "external_message_id": raced["external_message_id"],
                    "deduplicated": True,
                    "accepted_sim_time": raced["accepted_sim_time"],
                }
            row = cursor.execute(
                "SELECT COALESCE(MAX(accepted_sequence), 0) + 1 AS next_sequence "
                "FROM feishu_im_messages"
            ).fetchone()
            sequence = int(dict(row)["next_sequence"])
            cursor.execute(
                """
                INSERT INTO feishu_im_messages(
                    external_message_id, effect_id, conversation_id,
                    sender_actor_id, recipient_actor_ids, receive_id,
                    receive_id_type, effect_type, content,
                    accepted_sequence, accepted_sim_time, provider_deduplicated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_ids[0],
                    effect_id,
                    command["conversation_id"],
                    command["sender_actor_id"],
                    canonical_json(recipients),
                    canonical_json(open_ids) if len(open_ids) > 1 else open_ids[0],
                    "open_id",
                    command["effect_type"],
                    content,
                    sequence,
                    accepted_sim_time,
                    1 if provider_deduplicated else 0,
                ),
            )

        return {
            "external_message_id": message_ids[0],
            "deduplicated": provider_deduplicated,
            "accepted_sim_time": accepted_sim_time,
        }

    def messages(self) -> list[dict[str, Any]]:
        rows = self.database.all(
            "SELECT * FROM feishu_im_messages ORDER BY accepted_sequence"
        )
        return [dict(row) for row in rows]

    def message_for_effect(self, effect_id: str) -> dict[str, Any] | None:
        return self._existing_receipt(effect_id)

    def command_for_effect(self, effect_id: str) -> dict[str, Any] | None:
        """The notification an effect was built from, for redrawing its card.

        Read back from the Outbox rather than kept in memory: a ballot half
        filled in survives a restart because the message and the contract both
        outlive the process that sent them.
        """

        row = self.database.one(
            "SELECT effect_id, effect_type, payload FROM outbox_entries "
            "WHERE effect_id = ?",
            (effect_id,),
        )
        if not row:
            return None
        record = dict(row)
        payload = record["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        notification = (payload or {}).get("notification")
        if not notification:
            return None
        return {
            "effect_id": record["effect_id"],
            "effect_type": record["effect_type"],
            "notification": notification,
        }

    def update_card(self, effect_id: str, card: dict[str, Any]) -> bool:
        """Replace the card an effect produced. Returns whether one was found.

        Keyed on EffectId rather than on a message id the caller carries, so an
        update lands on the same message the send produced even after a restart
        -- the receipt row is the only place that mapping lives.
        """

        receipt = self._existing_receipt(effect_id)
        if not receipt:
            return False
        content = canonical_json(card)
        self.transport.update_message(
            message_id=receipt["external_message_id"], content=content
        )
        with self.database.transaction() as cursor:
            cursor.execute(
                "UPDATE feishu_im_messages SET content = ? WHERE effect_id = ?",
                (content, effect_id),
            )
        return True

    # ---- inbound ------------------------------------------------------

    def record_inbound_action(
        self,
        *,
        event_key: str,
        operator_open_id: str,
        action_name: str,
        effect_id: str | None,
        raw_value: dict[str, Any],
        sim_time: str,
    ) -> dict[str, Any]:
        """Durably park one card click for the worker to process.

        Feishu requires the callback to answer within 3 seconds, so the handler
        only writes this row and returns; nothing that can block runs inline.
        `event_key` carries Feishu's own event id, making a redelivered click a
        no-op rather than a second decision.
        """

        with self.database.transaction() as cursor:
            existing = cursor.execute(
                "SELECT action_id, status FROM feishu_inbound_actions "
                "WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if existing:
                existing = dict(existing)
                return {
                    "action_id": existing["action_id"],
                    "status": existing["status"],
                    "deduplicated": True,
                }
            action_id = f"fsa_{uuid4().hex}"
            actor_id = None
            binding = cursor.execute(
                "SELECT actor_id FROM feishu_identity_bindings WHERE open_id = ?",
                (operator_open_id,),
            ).fetchone()
            if binding:
                actor_id = dict(binding)["actor_id"]
            cursor.execute(
                """
                INSERT INTO feishu_inbound_actions(
                    action_id, event_key, operator_open_id, actor_id,
                    action_name, effect_id, raw_value, status, received_sim_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    action_id,
                    event_key,
                    operator_open_id,
                    actor_id,
                    action_name,
                    effect_id,
                    canonical_json(raw_value),
                    sim_time,
                ),
            )
        return {"action_id": action_id, "status": "PENDING", "deduplicated": False}

    def pending_inbound_actions(self) -> list[dict[str, Any]]:
        rows = self.database.all(
            "SELECT * FROM feishu_inbound_actions WHERE status = 'PENDING' "
            "ORDER BY received_sim_time, action_id"
        )
        return [dict(row) for row in rows]
