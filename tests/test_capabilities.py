from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from collab_agent.models import read_text_file


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "capabilities.json"

VALID_STATUSES = {
    "DONE",
    "DONE_WITH_EXTRA",
    # Built and covered by offline tests, but never run against the live
    # service it talks to. Kept distinct from DONE because "the tests pass" and
    # "it works against the real thing" are different claims, and collapsing
    # them is how a manifest starts lying.
    "DONE_UNVERIFIED",
    "NOT_DONE",
    "NOT_SUPPORTED",
    "WILL_NOT_DO",
    "MEASURED_REJECTED",
}
PROVEN_STATUSES = {"DONE", "DONE_WITH_EXTRA", "DONE_UNVERIFIED"}


def _load() -> dict:
    return json.loads(read_text_file(MANIFEST))


def _entries():
    for group, capabilities in _load()["capabilities"].items():
        for name, entry in capabilities.items():
            yield group, name, entry


class ManifestShapeTests(unittest.TestCase):
    """The manifest is the answer to 'is X done'. It has to be well formed."""

    def test_every_status_is_one_of_the_known_values(self) -> None:
        for group, name, entry in _entries():
            self.assertIn(
                entry["status"],
                VALID_STATUSES,
                f"{group}.{name} has status {entry['status']!r}",
            )

    def test_done_with_extra_always_names_the_extra(self) -> None:
        for group, name, entry in _entries():
            if entry["status"] == "DONE_WITH_EXTRA":
                self.assertTrue(
                    entry.get("requires_extra"),
                    f"{group}.{name} claims an extra is needed but does not say which",
                )

    def test_every_named_extra_exists_in_pyproject(self) -> None:
        pyproject = read_text_file(ROOT / "pyproject.toml")
        for group, name, entry in _entries():
            extra = entry.get("requires_extra")
            if extra:
                self.assertIn(
                    f"{extra} = [",
                    pyproject,
                    f"{group}.{name} needs extra {extra!r}, which pyproject does not define",
                )

    def test_every_verified_by_points_at_a_file_that_exists(self) -> None:
        for group, name, entry in _entries():
            target = entry.get("verified_by")
            if target:
                self.assertTrue(
                    (ROOT / target).exists(),
                    f"{group}.{name} cites {target}, which does not exist",
                )

    def test_a_done_capability_cites_its_proof(self) -> None:
        for group, name, entry in _entries():
            if entry["status"] in PROVEN_STATUSES:
                self.assertTrue(
                    entry.get("verified_by"),
                    f"{group}.{name} claims DONE without naming a test",
                )

    def test_an_unverified_capability_says_what_is_unverified(self) -> None:
        """DONE_UNVERIFIED must carry the caveat, or it reads as DONE."""

        for group, name, entry in _entries():
            if entry["status"] == "DONE_UNVERIFIED":
                self.assertIn(
                    "从未",
                    str(entry.get("note") or ""),
                    f"{group}.{name} is unverified but its note does not say so",
                )


class ManifestAgreesWithCodeTests(unittest.TestCase):
    """Claims are checked against reality, so the manifest cannot go stale
    the way the prose in colwrok_SDD/ did."""

    def test_office_formats_match_what_attachments_actually_handles(self) -> None:
        from collab_agent.attachments import OFFICE_FILE_SUFFIXES

        declared = {
            name
            for name, entry in _load()["capabilities"]["attachment_extraction"].items()
            if entry["status"] == "DONE_WITH_EXTRA"
        }

        self.assertEqual(
            declared,
            {suffix.lstrip(".").upper() for suffix in OFFICE_FILE_SUFFIXES},
        )

    def test_office_support_really_is_optional(self) -> None:
        """DONE_WITH_EXTRA means the import is guarded, not merely documented."""

        source = read_text_file(ROOT / "src/collab_agent/attachments.py")

        self.assertIn("except ImportError", source)
        self.assertIn("install markitdown", source)

    def test_the_rejected_tool_prompt_version_matches_the_code(self) -> None:
        from collab_agent.extraction import (
            ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
            ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
        )

        model_use = _load()["capabilities"]["model_use"]
        self.assertEqual(
            model_use["ACTION_ITEM_EXTRACTION"]["prompt_version"],
            ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
        )
        self.assertEqual(
            model_use["EXTRACTION_TOOL_CALLING"]["prompt_version"],
            ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
        )

    def test_pgvector_is_declared_unsupported_and_stays_uninstalled(self) -> None:
        entry = _load()["capabilities"]["storage_backends"]["PGVECTOR"]

        self.assertEqual(entry["status"], "NOT_SUPPORTED")
        self.assertIsNone(
            importlib.util.find_spec("pgvector"),
            "pgvector appeared as a dependency; the dual-backend claim needs revisiting",
        )

    def test_feishu_capabilities_match_the_modules_that_exist(self) -> None:
        surfaces = _load()["capabilities"]["interaction_surfaces"]
        done = {
            name for name, entry in surfaces.items() if entry["status"] == "DONE"
        }

        self.assertIn("FEISHU_LONG_CONNECTION", done)
        self.assertTrue((ROOT / "src/collab_agent/feishu_app.py").exists())
        self.assertEqual(
            surfaces["FEISHU_MINUTES_INTAKE"]["status"], "DONE_UNVERIFIED"
        )
        self.assertTrue((ROOT / "src/collab_agent/feishu_minutes.py").exists())

    def test_the_semantic_threshold_matches_the_code(self) -> None:
        from collab_agent.embeddings import SEMANTIC_LINK_THRESHOLD

        note = _load()["capabilities"]["model_use"]["SEMANTIC_SIMILARITY"]["note"]

        self.assertIn(str(SEMANTIC_LINK_THRESHOLD), note)


class NoStaleTestCountsTests(unittest.TestCase):
    """A test count in a spec is a build snapshot, not an architectural fact."""

    def test_the_manifest_states_no_test_counts(self) -> None:
        import re

        text = read_text_file(MANIFEST)

        self.assertIsNone(
            re.search(r"\d+\s*(tests|个测试)", text),
            "a count here would go stale on the next test added",
        )


if __name__ == "__main__":
    unittest.main()
