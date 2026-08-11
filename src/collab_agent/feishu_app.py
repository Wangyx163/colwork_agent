from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .feishu_cards import build_ack_card
from .feishu_commands import _payload_of
from .feishu_config import FeishuConfig
from .feishu_registration import REGISTRATION_REVOKE_ACTION as REGISTRATION_REVOKE
from .feishu_im import FeishuIM
from .models import canonical_json, effect_id


def real_now() -> str:
    """Wall-clock timestamp for the live Feishu runtime.

    The deterministic evaluation keeps using `VirtualClock`; a live tenant has
    no simulated time to advance, so the two never share a clock.
    """

    return datetime.now(timezone.utc).isoformat()


def flushing_log(line: str) -> None:
    """Print without buffering.

    This process runs for hours waiting on a socket, so a buffered stdout would
    show nothing at all until the buffer happened to fill -- which reads exactly
    like a hung connection.
    """

    print(line, flush=True)


class FeishuApp:
    """Long-connection Feishu entry point for the coordination agent.

    Both callbacks obey Feishu's 3-second budget by writing durably and
    returning immediately. Everything that can block — rendering, sending,
    advancing domain state — happens on the worker thread, which is the same
    split the Outbox already enforces for outbound effects.
    """

    def __init__(
        self,
        config: FeishuConfig,
        im: FeishuIM,
        *,
        episode_id: str = "episode_feishu_mvp",
        on_action: Callable[[dict[str, Any]], None] | None = None,
        registrar: Any | None = None,
        log: Callable[[str], None] = flushing_log,
    ) -> None:
        self.config = config
        self.im = im
        self.episode_id = episode_id
        self.on_action = on_action
        # Off unless injected, so every existing caller and every offline test
        # keeps the read-only reply they had.
        self.registrar = registrar
        self.log = log
        self._work: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- callbacks ----------------------------------------------------

    def handle_message_receive(self, data: Any) -> None:
        """Park an inbound message and return well inside the 3-second budget."""

        try:
            event = data.event
            sender_open_id = event.sender.sender_id.open_id
            message = event.message
            message_id = message.message_id
            text = ""
            if message.content:
                try:
                    text = json.loads(message.content).get("text", "")
                except (ValueError, AttributeError):
                    text = str(message.content)
            self._work.put(
                (
                    "ACK_MESSAGE",
                    {
                        "sender_open_id": sender_open_id,
                        "message_id": message_id,
                        "text": text,
                        # Carried so a person can discover it: chat_id is
                        # required to pull a roster from a group, and there is
                        # no other place to read it without an API call that
                        # itself needs the id.
                        "chat_id": getattr(message, "chat_id", "") or "",
                        "chat_type": getattr(message, "chat_type", "") or "",
                    },
                )
            )
        except Exception as error:  # noqa: BLE001 - never let a callback raise
            self.log(f"[feishu] message callback error: {error!r}")

    def handle_card_action(self, data: Any) -> Any:
        """Durably record a card click, answer with a toast, process later."""

        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        try:
            event = data.event
            operator_open_id = event.operator.open_id
            raw_value = event.action.value or {}
            if isinstance(raw_value, str):
                try:
                    raw_value = json.loads(raw_value)
                except ValueError:
                    raw_value = {"raw": raw_value}
            action_name = str(raw_value.get("action", "unknown"))
            clicked_effect_id = raw_value.get("effect_id")
            # A `select_static` reports the picked option separately from the
            # element's own value. Folding it in here keeps everything the
            # decision needs inside the one durable row.
            option = getattr(event.action, "option", None)
            if option:
                raw_value = {**raw_value, "reason": str(option)}
            # Feishu's own event id makes a redelivered click idempotent; the
            # fallback keeps a click recordable even if the field is absent.
            event_key = (
                getattr(data.header, "event_id", None)
                or f"{operator_open_id}:{clicked_effect_id}:{action_name}"
            )
            outcome = self.im.record_inbound_action(
                event_key=event_key,
                operator_open_id=operator_open_id,
                action_name=action_name,
                effect_id=clicked_effect_id,
                raw_value=raw_value,
                sim_time=real_now(),
            )
            if not outcome["deduplicated"]:
                self._work.put(("PROCESS_ACTION", {"action_id": outcome["action_id"]}))
            toast_text = (
                "已收到，正在处理" if not outcome["deduplicated"] else "这一步已经处理过了"
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "info" if not outcome["deduplicated"] else "warning",
                        "content": toast_text,
                    }
                }
            )
        except Exception as error:  # noqa: BLE001 - never let a callback raise
            self.log(f"[feishu] card callback error: {error!r}")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "处理失败，请稍后重试"}}
            )

    # ---- worker -------------------------------------------------------

    def _ack_message(self, job: dict[str, Any]) -> None:
        """Answer an inbound chat message.

        Only ever a read-only reply. Decision cards are pushed by the notifier
        from real pending assignments; a card minted here would carry an
        EffectId that resolves to no task, so clicking it could only fail.
        """

        sender_open_id = job["sender_open_id"]
        if self.registrar is not None:
            self._register_from_message(job)
            return
        actor_id = self.im.actor_for_open_id(sender_open_id)
        if actor_id is None:
            # The bootstrap path: someone has to be able to discover their own
            # open_id before a coordinator can bind them.
            self._send_raw_card(
                sender_open_id,
                build_ack_card(
                    title="尚未绑定",
                    body=(
                        "你还没有绑定到本次会议的参会名单。\n"
                        f"请把这个 open_id 交给会议负责人完成绑定：`{sender_open_id}`"
                    ),
                    template="orange",
                ),
                uuid_seed=f"unbound:{job['message_id']}",
            )
            self.log(f"[feishu] unbound sender {sender_open_id} asked for binding")
            return

        # In a group, the chat_id is echoed back: pulling a roster out of a
        # group needs it, and there is nowhere else to read it without an API
        # call that already requires the id. In a direct message there is no
        # roster to pull, so the line would only be noise.
        body = (
            "你已绑定到本次会议。派发和待办会自动推送到这里，无需回复。\n"
            "修订任务、提交成果和验收请在网页工作台完成。"
        )
        chat_id = str(job.get("chat_id") or "")
        if chat_id and str(job.get("chat_type") or "") == "group":
            body += f"\n\n本群 chat_id：`{chat_id}`\n（拉取参会名单时要用）"
        self._send_raw_card(
            sender_open_id,
            build_ack_card(title="已绑定", body=body),
            uuid_seed=f"bound:{job['message_id']}",
        )
        self.log(
            f"[feishu] acked bound sender {actor_id}"
            + (f" chat_id={chat_id}" if chat_id else "")
        )

    def _register_from_message(self, job: dict[str, Any]) -> None:
        """Bind whoever wrote in, or tell them precisely why not.

        Failures answer with a card too. The previous flow handed back an
        open_id and left the person to find a coordinator with a terminal, and
        a self-service path that says only "格式不对" is the same dead end with
        fewer words.
        """

        sender_open_id = job["sender_open_id"]
        try:
            outcome = self.registrar.handle_message(
                open_id=sender_open_id,
                text=str(job.get("text") or ""),
                sim_time=real_now(),
            )
        except Exception as error:  # noqa: BLE001 - a callback never raises
            self.log(f"[feishu] registration failed: {error!r}")
            return
        self._send_raw_card(
            sender_open_id,
            outcome["card"],
            uuid_seed=f"register:{job['message_id']}",
        )
        for open_id, card, seed in outcome.get("notify") or []:
            # The coordinator's copy carries the undo. Sent on the same thread
            # as the reply so the two cannot get out of order -- being told
            # somebody joined after they have already acted on it is worse than
            # being told slowly.
            self._send_raw_card(open_id, card, uuid_seed=seed)
        self.log(
            f"[feishu] registration from {sender_open_id}: "
            + ("bound " + str(outcome.get("actor_id")) if outcome.get("bound") else "refused")
        )

    def _send_raw_card(
        self, open_id: str, card: dict[str, Any], *, uuid_seed: str
    ) -> None:
        message_id, _ = self.im.transport.send_message(
            receive_id=open_id,
            receive_id_type="open_id",
            msg_type="interactive",
            content=canonical_json(card),
            uuid=effect_id(
                episode_id=self.episode_id,
                subject_id=open_id,
                effect_type="RAW_CARD",
                trigger_key=uuid_seed,
            ),
        )
        self.log(f"[feishu] sent raw card to {open_id} -> {message_id}")

    def _process_action(self, job: dict[str, Any]) -> None:
        action_id = job["action_id"]
        row = self.im.database.one(
            "SELECT * FROM feishu_inbound_actions WHERE action_id = ?", (action_id,)
        )
        if row is None:
            self.log(f"[feishu] action {action_id} vanished before processing")
            return
        record = dict(row)
        if record["status"] != "PENDING":
            return
        error: str | None = None
        outcome: Any = None
        try:
            if (
                self.registrar is not None
                and record["action_name"] == REGISTRATION_REVOKE
            ):
                # Not routed to a meeting: a binding belongs to a person, not
                # to an episode, and the router resolves by task -- which this
                # click has none of.
                outcome = self.registrar.revoke(
                    actor_id=_payload_of(record).get("actor_id", ""),
                    by_open_id=record["operator_open_id"],
                )
            elif self.on_action is not None:
                outcome = self.on_action(record)
            else:
                self.log(
                    f"[feishu] action {record['action_name']} by "
                    f"{record['actor_id'] or record['operator_open_id']} "
                    f"on effect {record['effect_id']}"
                )
        except Exception as exc:  # noqa: BLE001 - failure must be recorded, not lost
            error = repr(exc)
        # Scoring one option of a ballot is not a decision -- nothing has been
        # expressed until the whole thing is submitted -- so the card is
        # redrawn with the running scores instead of being closed.
        if (
            error is None
            and isinstance(outcome, dict)
            and outcome.get("status") == "SCORING"
        ):
            self._redraw_ballot(record, outcome.get("scores") or {})
            return
        # Only now is the outcome known: the callback merely parked the click,
        # and the domain can still refuse it. Updating the card here rather
        # than in the callback is what keeps it from claiming an acceptance
        # that did not happen.
        self._settle_card(record, error)
        with self.im.database.transaction() as cursor:
            cursor.execute(
                "UPDATE feishu_inbound_actions SET status = ?, "
                "processed_sim_time = ?, process_error = ? WHERE action_id = ?",
                (
                    "FAILED" if error else "PROCESSED",
                    real_now(),
                    error,
                    action_id,
                ),
            )
        if error:
            self.log(f"[feishu] action {action_id} failed: {error}")

    def _redraw_ballot(self, record: dict[str, Any], scores: dict[str, Any]) -> None:
        """Put the running scores back on the card the click came from.

        The card is the form, so the partial ballot has to live in the message:
        rebuilt from the notification the effect carried, with what has been
        picked so far folded in. A pick that cannot be redrawn is silently
        lost, so a failure here is logged rather than swallowed.
        """

        from .feishu_cards import build_notification_card

        effect_id = record.get("effect_id")
        if not effect_id:
            return
        command = self.im.command_for_effect(str(effect_id))
        if not command:
            self.log(f"[feishu] no ballot behind effect {effect_id}; pick lost")
            return
        try:
            self.im.update_card(
                str(effect_id), build_notification_card(command, scores)
            )
        except Exception as error:  # noqa: BLE001 - a lost pick must be visible
            self.log(f"[feishu] could not redraw ballot {effect_id}: {error!r}")

    def _settle_card(self, record: dict[str, Any], error: str | None) -> None:
        """Rewrite the card to what actually happened, buttons removed."""

        from .feishu_cards import build_decided_card
        from .feishu_commands import DECISIONS

        effect_id = record.get("effect_id")
        if not effect_id:
            return
        original = self.im.message_for_effect(str(effect_id))
        if not original:
            return
        try:
            body = json.loads(original["content"])
            content = body["elements"][0]["text"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            content = ""

        if error:
            card = build_decided_card(
                content,
                decision="FAILED",
                reason="处理失败，请在网页工作台完成这一步",
                footer=str(error)[:80],
            )
        else:
            reason = ""
            try:
                reason = str(json.loads(record.get("raw_value") or "{}").get("reason") or "")
            except ValueError:
                reason = ""
            card = build_decided_card(
                content,
                decision=DECISIONS.get(str(record.get("action_name")), ""),
                reason=reason,
            )
        try:
            self.im.update_card(str(effect_id), card)
        except Exception as exc:  # noqa: BLE001 - a stale card must not undo the decision
            self.log(f"[feishu] card update failed (decision stands): {exc!r}")

    def drain_once(self, *, timeout: float = 0.5) -> bool:
        """Process one queued job. Returns False when the queue was empty."""

        try:
            kind, job = self._work.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            if kind == "ACK_MESSAGE":
                self._ack_message(job)
            elif kind == "PROCESS_ACTION":
                self._process_action(job)
            else:
                self.log(f"[feishu] unknown job kind {kind}")
        except Exception as error:  # noqa: BLE001 - worker must survive one bad job
            self.log(f"[feishu] worker job {kind} failed: {error!r}")
        finally:
            self._work.task_done()
        return True

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self.drain_once()

    def start_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="feishu-worker", daemon=True
        )
        self._worker.start()

    def stop_worker(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None

    # ---- runtime ------------------------------------------------------

    def build_event_handler(self) -> Any:
        import lark_oapi as lark

        return (
            lark.EventDispatcherHandler.builder(
                self.config.encrypt_key, self.config.verification_token
            )
            .register_p2_im_message_receive_v1(self.handle_message_receive)
            .register_p2_card_action_trigger(self.handle_card_action)
            .build()
        )

    def run(self) -> None:
        """Open the long connection and serve until interrupted.

        Long connection needs no public IP, no tunnel and no signature
        verification of its own, which is why it is the local default.
        """

        import lark_oapi as lark

        self.start_worker()
        client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=self.build_event_handler(),
            log_level=lark.LogLevel.INFO,
        )
        self.log(
            "[feishu] long connection starting; "
            f"config={json.dumps(self.config.redacted(), ensure_ascii=False)}"
        )
        try:
            client.start()
        finally:
            self.stop_worker()
