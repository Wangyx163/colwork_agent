from __future__ import annotations

import ast
import unittest
from pathlib import Path

from collab_agent.user_messages import USER_MESSAGES, user_message


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "collab_agent"


def raised_messages() -> set[str]:
    """Every literal a domain refusal can carry.

    Parsed rather than pattern-matched: these strings are routinely written
    across several lines and joined by implicit concatenation, which a regex
    reads as the first fragment only -- and then reports a perfectly good
    translation as orphaned. That happened on the first run of this test.
    """

    found: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            for argument in node.exc.args:
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    found.add(argument.value)
    return found


class UserFacingMessageTests(unittest.TestCase):
    """A refusal the reader cannot act on is a refusal that reads as a bug.

    The domain raises in English on purpose -- those strings name the rule and
    are what a log or a test should carry. What reaches a person is a different
    job, and until it was done every click that hit a rule produced a sentence
    about an enum they had never seen.
    """

    def test_a_translated_refusal_says_what_to_do(self) -> None:
        shown = user_message(
            ValueError(
                "contribution action must be INCLUDE, REQUEST_REVISION, or PROMOTE"
            )
        )

        self.assertNotIn("INCLUDE", shown)
        self.assertIn("采纳", shown)

    def test_an_untranslated_one_keeps_its_english(self) -> None:
        """Losing it behind "操作失败" would take away the only clue there was."""

        self.assertEqual(
            user_message(ValueError("a rule nobody has translated yet")),
            "a rule nobody has translated yet",
        )

    def test_every_translation_still_matches_something_raised(self) -> None:
        """A translation for a message that no longer exists is dead weight,
        and worse, it hides that the rule it described has changed."""

        raised = raised_messages()
        orphaned = sorted(
            english for english in USER_MESSAGES if english not in raised
        )

        self.assertEqual(
            orphaned, [], "these translations no longer match anything raised"
        )

    def test_the_translations_are_actually_chinese(self) -> None:
        for english, chinese in USER_MESSAGES.items():
            self.assertTrue(
                any("一" <= char <= "鿿" for char in chinese),
                f"{english} was not translated",
            )


if __name__ == "__main__":
    unittest.main()
