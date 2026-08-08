from __future__ import annotations

from typing import Any

from .feishu_im import FeishuIdentityUnbound
from .models import effect_id


PENDING_ASSIGNMENT_SQL = """
SELECT a.assignment_id, a.action_item_id, a.definition_version, a.actor_id,
       a.assignment_role, a.assignment_message,
       i.title, i.team_required_by_sim_time
FROM action_item_assignments a
JOIN action_items i ON i.action_item_id = a.action_item_id
WHERE i.episode_id = ?
  AND a.response_status = 'PENDING'
  AND i.status = 'PENDING_ASSIGNMENT'
ORDER BY a.assigned_sim_time, a.assignment_id
"""

ROLE_LABELS = {"OWNER": "主负责人", "COLLABORATOR": "协作者"}


class AssignmentNotifier:
    """Pushes a card for each pending assignment that has not been sent yet.

    This project's dispatch is pull-based: `dispatch_action` records who is
    assigned but enqueues no Outbox effect, because the web workbench expects
    people to come and look at their task page. Feishu has to push instead, so
    this projects the pending assignments into IM effects.

    It is a projection, not a domain change. The EffectId is derived from the
    assignment's own natural key -- the task, the definition version and the
    person -- so a restart, a redelivery, or a second poll all resolve to the
    same effect and FeishuIM refuses to send it twice. Redispatching after a
    revision bumps `definition_version`, which is a genuinely new effect and
    should reach the person again.
    """

    def __init__(self, service: Any, im: Any, *, log: Any = None) -> None:
        self.service = service
        self.im = im
        self.log = log
        # An unbound assignee is a standing condition, not an event: the poll
        # meets it again every couple of seconds and would otherwise print the
        # same line until somebody binds them. Logged once per person, and
        # cleared if a binding appears so a later regression is still visible.
        self._reported_unbound: set[str] = set()

    def _display_name(self, actor_id: str) -> str:
        row = self.service.db.one(
            "SELECT display_name FROM actors WHERE actor_id = ?", (actor_id,)
        )
        return dict(row)["display_name"] if row else actor_id

    def pending(self) -> list[dict[str, Any]]:
        rows = self.service.db.all(
            PENDING_ASSIGNMENT_SQL, (self.service.episode_id,)
        )
        return [dict(row) for row in rows]

    def _command(self, assignment: dict[str, Any]) -> dict[str, Any]:
        role = ROLE_LABELS.get(
            str(assignment["assignment_role"]), str(assignment["assignment_role"])
        )
        due = assignment.get("team_required_by_sim_time") or "未设定"
        message = str(assignment.get("assignment_message") or "").strip()
        content = (
            f"**{assignment['title']}**\n\n"
            f"你的角色：{role}\n"
            f"团队要求完成时间：{due}"
        )
        if message:
            content += f"\n\n负责人说明：{message}"
        return {
            "effect_id": effect_id(
                episode_id=self.service.episode_id,
                subject_id=str(assignment["assignment_id"]),
                effect_type="ASSIGNMENT_REQUEST",
                trigger_key=str(assignment["definition_version"]),
            ),
            "effect_type": "ASSIGNMENT_REQUEST",
            "conversation_id": "conv_feishu",
            "sender_actor_id": "agent",
            "recipient_actor_ids": [str(assignment["actor_id"])],
            # Carried on the card because there is no Outbox row to resolve
            # through. A stale card is still safe: the domain re-checks the
            # task status and the assignment before accepting any decision.
            "action_item_id": str(assignment["action_item_id"]),
            "content": content,
        }

    def notify_once(self) -> dict[str, Any]:
        """Send every not-yet-sent pending assignment. Safe to call in a loop."""

        sent: list[str] = []
        skipped: list[dict[str, str]] = []
        for assignment in self.pending():
            command = self._command(assignment)
            actor_id = str(assignment["actor_id"])
            try:
                receipt = self.im.send(
                    command, accepted_sim_time=self.service.now()
                )
            except FeishuIdentityUnbound as error:
                # Not fatal for the poll: other people's cards must still go
                # out, and the binding may appear before the next round.
                skipped.append(
                    {
                        "assignment_id": str(assignment["assignment_id"]),
                        "actor_id": actor_id,
                        "display_name": self._display_name(actor_id),
                        "reason": str(error),
                        "first_report": actor_id not in self._reported_unbound,
                    }
                )
                self._reported_unbound.add(actor_id)
                continue
            self._reported_unbound.discard(actor_id)
            if not receipt["deduplicated"]:
                sent.append(command["effect_id"])
                if self.log is not None:
                    self.log(
                        f"[feishu] assignment card -> {assignment['actor_id']} "
                        f"({assignment['title']})"
                    )
        return {"sent": sent, "skipped": skipped}
