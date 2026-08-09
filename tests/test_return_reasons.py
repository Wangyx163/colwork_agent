from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from collab_agent.feishu_cards import RETURN_REASONS, build_notification_card
from collab_agent.models import ASSIGNMENT_RETURN_REASONS, OTHER_RETURN_REASON
from collab_agent.service import CoordinationService


WEB_SRC = Path(__file__).resolve().parents[1] / "web" / "src"


class ReturnReasonParityTests(unittest.TestCase):
    """The web page and the Feishu card must offer the identical list.

    A reason recorded from one surface is read back from the other, so a list
    that differs between them turns the same decision into two incomparable
    ones. The server-rendered page held this by string substitution; the React
    page holds it by taking the list from `state.vocabulary` at runtime, which
    is what these tests pin down -- including that nobody has quietly pasted a
    copy back into the TypeScript.
    """

    def test_the_card_offers_exactly_the_canonical_reasons(self) -> None:
        self.assertEqual(list(RETURN_REASONS), list(ASSIGNMENT_RETURN_REASONS))

    def test_the_card_renders_every_reason(self) -> None:
        card = build_notification_card(
            {
                "effect_id": "eff_1",
                "effect_type": "ASSIGNMENT_RESPONSE_REQUIRED",
                "notification": {
                    "notification_contract_version": "notification.v1",
                    "kind": "ASSIGNMENT_RESPONSE_REQUIRED",
                    "action_item_id": "ai_1",
                    "subject_id": "asg_1",
                    "title": "任务派发",
                    "summary": "请回应",
                    "fields": [],
                    "decisions": [
                        {
                            "name": "ASSIGNMENT_RETURN",
                            "label": "退回重改",
                            "requires_reason": True,
                        }
                    ],
                    "deep_link_path": "/tasks",
                },
            }
        )
        rendered = json.dumps(card, ensure_ascii=False)

        for reason in ASSIGNMENT_RETURN_REASONS:
            self.assertIn(reason, rendered)

    def test_the_page_takes_the_list_from_the_server(self) -> None:
        source = (WEB_SRC / "tasks" / "MyTaskCard.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("vocabulary.return_reasons", source)
        for reason in (*ASSIGNMENT_RETURN_REASONS, OTHER_RETURN_REASON):
            self.assertNotIn(
                reason,
                source,
                "the reasons must come from state.vocabulary, not a copy",
            )

    def test_the_page_takes_the_other_vocabularies_too(self) -> None:
        """A button offering a value the domain rejects is a defect users find."""

        source = (WEB_SRC / "tasks" / "MyTaskCard.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("vocabulary.quick_signals", source)
        self.assertIn("vocabulary.assistance_categories", source)
        # Labels are local because the server stores codes; a *list* is not.
        listed = re.findall(r"^\s*const [A-Z_]+ = \[", source, re.MULTILINE)
        self.assertEqual(listed, [], "no vocabulary list may live in the page")

    def test_every_code_the_page_can_label_is_one_the_domain_accepts(
        self,
    ) -> None:
        source = (WEB_SRC / "tasks" / "MyTaskCard.tsx").read_text(
            encoding="utf-8"
        )
        block = source.split("const SIGNAL_LABEL")[1].split("};")[0]
        labelled = set(re.findall(r"^\s+([A-Z_]+):", block, re.MULTILINE))

        self.assertEqual(labelled, set(CoordinationService.QUICK_SIGNAL_TYPES))


if __name__ == "__main__":
    unittest.main()
