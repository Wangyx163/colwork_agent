from __future__ import annotations

import json
import unittest

from collab_agent.feishu_cards import RETURN_REASONS, build_effect_card
from collab_agent.models import ASSIGNMENT_RETURN_REASONS, OTHER_RETURN_REASON
from collab_agent.web import WORKBENCH_HTML


class ReturnReasonParityTests(unittest.TestCase):
    """The web workbench and the Feishu card must offer the identical list.

    A reason recorded from one surface is read back from the other, so a list
    that drifts would leave returns that cannot be reproduced or explained.
    """

    def test_the_shared_list_is_non_empty(self) -> None:
        self.assertTrue(ASSIGNMENT_RETURN_REASONS)
        self.assertEqual(
            len(set(ASSIGNMENT_RETURN_REASONS)),
            len(ASSIGNMENT_RETURN_REASONS),
            "duplicate reasons would be indistinguishable once stored",
        )

    def test_feishu_card_offers_exactly_the_shared_list(self) -> None:
        card = build_effect_card(
            {
                "effect_id": "eff_1",
                "effect_type": "ASSIGNMENT_REQUEST",
                "content": "请确认",
            }
        )
        picker = [
            action
            for element in card["elements"]
            if element["tag"] == "action"
            for action in element["actions"]
            if action["tag"] == "select_static"
        ][0]

        self.assertEqual(
            [option["value"] for option in picker["options"]],
            list(ASSIGNMENT_RETURN_REASONS),
        )

    def test_workbench_page_ships_exactly_the_shared_list(self) -> None:
        marker = "const RETURN_REASONS="
        start = WORKBENCH_HTML.index(marker) + len(marker)
        end = WORKBENCH_HTML.index(";", start)
        shipped = json.loads(WORKBENCH_HTML[start:end])

        self.assertEqual(shipped, list(ASSIGNMENT_RETURN_REASONS))

    def test_only_the_web_offers_a_free_text_option(self) -> None:
        """A card picker cannot collect free text in the same tap, so offering
        「其他」 there would produce a return with no reason -- which the domain
        refuses."""

        self.assertIn(OTHER_RETURN_REASON, WORKBENCH_HTML)
        self.assertNotIn(OTHER_RETURN_REASON, RETURN_REASONS)

        card = build_effect_card(
            {
                "effect_id": "eff_1",
                "effect_type": "ASSIGNMENT_REQUEST",
                "content": "请确认",
            }
        )
        picker = [
            action
            for element in card["elements"]
            if element["tag"] == "action"
            for action in element["actions"]
            if action["tag"] == "select_static"
        ][0]
        self.assertNotIn(
            OTHER_RETURN_REASON, [option["value"] for option in picker["options"]]
        )

    def test_choosing_other_reads_the_free_text_box(self) -> None:
        self.assertIn("function returnReasonFor(", WORKBENCH_HTML)
        self.assertIn("-other`", WORKBENCH_HTML)
        self.assertIn("toggleOtherReason", WORKBENCH_HTML)

    def test_the_popover_closes_on_an_outside_click(self) -> None:
        self.assertIn("panel.contains(e.target)", WORKBENCH_HTML)
        self.assertIn("Escape", WORKBENCH_HTML)

    def test_no_placeholder_survives_into_the_served_page(self) -> None:
        self.assertNotIn("__RETURN_REASONS__", WORKBENCH_HTML)

    def test_feishu_module_re_exports_rather_than_redefines(self) -> None:
        self.assertIs(RETURN_REASONS, ASSIGNMENT_RETURN_REASONS)

    def test_workbench_return_button_reads_the_reason_select(self) -> None:
        self.assertIn("-reason", WORKBENCH_HTML)
        self.assertIn(
            "decision==='RETURN_FOR_REVISION'?returnReasonFor(id):note",
            WORKBENCH_HTML,
            "a return must take the reason control, not the accept note",
        )


if __name__ == "__main__":
    unittest.main()
