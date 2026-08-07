from __future__ import annotations

import unittest

from collab_agent.context_budget import (
    ContextBudgetError,
    bound_collaboration_hint_context,
    bound_task_result_context,
    estimate_tokens,
)
from collab_agent.task_result_processing import build_task_result_context


def task_context(
    *,
    attachment_text: str = "evidence",
    previous_versions: list[dict] | None = None,
    processing_purpose: str = "TASK_RESULT_REVIEW",
) -> dict:
    return build_task_result_context(
        action_item_id="task_1",
        title="Prepare interview demo",
        deliverable="A verified demo",
        acceptance_criteria="Shows recovery and lineage",
        source_timestamp="00:01:00",
        source_quote="Prepare the demo",
        version_id="version_current",
        payload={"summary": "Done", "content": "Current result", "links": []},
        attachments=[
            {
                "name": "result.txt",
                "type": "text/plain",
                "size": len(attachment_text),
                "extraction_status": "EXTRACTED",
                "text_characters": len(attachment_text),
                "extracted_text": attachment_text,
            }
        ],
        previous_versions=previous_versions or [],
        processing_purpose=processing_purpose,
    )


class ContextBudgetTests(unittest.TestCase):
    def test_collaboration_hint_drops_oldest_memory_deterministically(self) -> None:
        context = {
            "purpose": "COLLABORATION_HINT",
            "action_item_id": "task_1",
            "subject_actor_id": "actor_1",
            "confirmed_memories": [
                {
                    "memory_id": "memory_old",
                    "topic": "CADENCE",
                    "statement": "old " * 500,
                    "version": 1,
                    "evidence_refs": ["event:old"],
                },
                {
                    "memory_id": "memory_new",
                    "topic": "FORMAT",
                    "statement": "Prefer a concise checklist.",
                    "version": 2,
                    "evidence_refs": ["event:new"],
                },
            ],
            "usage_rules": {
                "may": ["adapt collaboration"],
                "must_not": ["decide acceptance"],
            },
        }
        context_without_old = {
            **context,
            "confirmed_memories": [context["confirmed_memories"][1]],
        }
        budget = estimate_tokens(context_without_old) + 5

        bounded, manifest = bound_collaboration_hint_context(
            context, token_budget=budget
        )

        self.assertEqual(
            [item["memory_id"] for item in bounded["confirmed_memories"]],
            ["memory_new"],
        )
        self.assertEqual(manifest["purpose"], "COLLABORATION_HINT")
        self.assertEqual(
            manifest["truncation_strategy"], ["DROP_OLDEST_CONFIRMED_MEMORY"]
        )
        self.assertIn("memory:memory_old:v1", manifest["omitted_refs"])
        self.assertIn("memory:memory_new:v2", manifest["included_refs"])

    def test_small_context_is_unchanged_and_manifested(self) -> None:
        original = task_context()
        bounded, manifest = bound_task_result_context(original, token_budget=10_000)

        self.assertEqual(bounded, original)
        self.assertFalse(manifest["truncated"])
        self.assertIn("task:contract", manifest["included_refs"])
        self.assertIn("submission:claim", manifest["included_refs"])
        self.assertIn("attachment:0", manifest["included_refs"])
        self.assertLessEqual(manifest["estimated_input_tokens"], 10_000)

    def test_manifest_uses_the_bound_context_purpose(self) -> None:
        _, manifest = bound_task_result_context(
            task_context(processing_purpose="CONTRIBUTION_ANALYSIS"),
            token_budget=10_000,
        )
        self.assertEqual(manifest["purpose"], "CONTRIBUTION_ANALYSIS")

    def test_old_versions_are_dropped_before_current_evidence(self) -> None:
        original = task_context(
            attachment_text="current evidence",
            previous_versions=[
                {"version_id": "version_old", "review_comment": "x" * 30_000}
            ],
        )
        without_history = task_context(attachment_text="current evidence")
        budget = estimate_tokens(without_history) + 5

        bounded, manifest = bound_task_result_context(original, token_budget=budget)

        self.assertEqual(bounded["previous_version_context"], [])
        self.assertEqual(
            bounded["evidence"]["attachments"][0]["extracted_text"],
            "current evidence",
        )
        self.assertIn("version:version_old", manifest["omitted_refs"])
        self.assertEqual(
            manifest["truncation_strategy"], ["DROP_OLDEST_PREVIOUS_VERSION"]
        )

    def test_attachment_tail_is_truncated_deterministically(self) -> None:
        text = "证据" * 20_000
        original = task_context(attachment_text=text)
        empty_attachment = task_context(attachment_text="")
        budget = estimate_tokens(empty_attachment) + 50

        first, first_manifest = bound_task_result_context(
            original, token_budget=budget
        )
        second, second_manifest = bound_task_result_context(
            original, token_budget=budget
        )

        kept = first["evidence"]["attachments"][0]["extracted_text"]
        self.assertTrue(text.startswith(kept))
        self.assertLess(len(kept), len(text))
        self.assertEqual(first, second)
        self.assertEqual(first_manifest, second_manifest)
        self.assertIn("attachment:0:text_tail", first_manifest["omitted_refs"])
        self.assertLessEqual(estimate_tokens(first), budget)

    def test_mandatory_context_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContextBudgetError, "mandatory"):
            bound_task_result_context(task_context(), token_budget=1)


if __name__ == "__main__":
    unittest.main()
