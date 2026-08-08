from __future__ import annotations

import json
import unittest
from pathlib import Path

from collab_agent.models import read_text_file
from collab_agent.observatory import build_observatory
from collab_agent.store import Database


ROOT = Path(__file__).resolve().parents[1]
P0_DB = ROOT / "var" / "p0.sqlite3"
P0_REPORT = ROOT / "var" / "report.json"


@unittest.skipUnless(
    P0_DB.exists() and P0_REPORT.exists(),
    "run `collab-agent eval --fresh` first to produce var/p0.sqlite3",
)
class AgreesWithTheCanonicalReportTests(unittest.TestCase):
    """The page must not become a second source of truth.

    Every number the Observatory shows also exists in report.json, computed by
    metrics.py from the same tables. If the two ever disagree, one of them is
    lying to a reader who has no way to tell which -- so they are compared
    directly rather than trusted to stay in step.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(P0_DB)
        cls.view = build_observatory(
            cls.db, episode_id="episode_p0", run_id="run_p0"
        )
        cls.report = json.loads(read_text_file(P0_REPORT))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_outbox_counts_match(self) -> None:
        canonical = self.report["node_signals"]["SIG-OUTBOX-001"]
        outbox = self.view["outbox"]

        self.assertEqual(outbox["created"], canonical["created"])
        self.assertEqual(outbox["claimed"], canonical["claimed"])
        self.assertEqual(outbox["delivered"], canonical["delivered"])
        self.assertEqual(outbox["dead_letter"], canonical["dead_letter"])
        self.assertEqual(
            outbox["deduplicated"], canonical["adapter_deduplicated"]
        )

    def test_zero_duplicates_agrees_with_the_gate(self) -> None:
        """delivered - created is not the definition; the gate's is."""

        gate_passed = self.report["gate_summary"]["GATE-DUP-001"]["passed"]

        self.assertEqual(self.view["headline"]["duplicate_sends"] == 0, gate_passed)

    def test_result_counts_match(self) -> None:
        canonical = self.report["node_signals"]["SIG-RESULT-001"]
        results = self.view["results"]

        self.assertEqual(results["received"], canonical["versions_received"])
        self.assertEqual(results["accepted"], canonical["accepted_task_results"])
        self.assertEqual(
            results["validation_failed"], canonical["validation_failed_versions"]
        )

    def test_audit_total_is_the_table_not_a_recount(self) -> None:
        """The headline number must be the audit log's own length."""

        rows = dict(
            self.db.one(
                "SELECT count(*) AS n FROM audit_events WHERE run_id = ?",
                ("run_p0",),
            )
        )

        self.assertEqual(self.view["audit"]["total"], rows["n"])

    def test_the_range_the_report_covers_is_unchanged(self) -> None:
        """Scoped to the report's own range on purpose.

        report.json froze the run at the moment `eval` produced it. Serving
        the demo database afterwards appends genuine audit events -- opening
        the workbench issues a session, and that is a real thing that
        happened. Comparing totals would therefore fail for the ordinary
        reason that somebody used the app, which says nothing about whether
        the Observatory agrees with the canonical metrics. What must hold is
        that everything the report counted is still there, untouched.
        """

        covered = self.report["evidence_refs"]["audit_sequence"]
        still_there = dict(
            self.db.one(
                "SELECT count(*) AS n, min(sequence_no) AS lo "
                "FROM audit_events WHERE run_id = ? AND sequence_no <= ?",
                ("run_p0", covered["last"]),
            )
        )

        self.assertEqual(still_there["n"], covered["count"])
        self.assertEqual(still_there["lo"], covered["first"])
        self.assertGreaterEqual(self.view["audit"]["total"], covered["count"])


@unittest.skipUnless(P0_DB.exists(), "needs var/p0.sqlite3")
class ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(P0_DB)
        cls.view = build_observatory(
            cls.db, episode_id="episode_p0", run_id="run_p0"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_every_audit_event_lands_in_exactly_one_lane(self) -> None:
        placed = sum(lane["count"] for lane in self.view["audit"]["lanes"])

        self.assertEqual(placed, self.view["audit"]["total"])

    def test_lane_positions_stay_inside_the_sequence(self) -> None:
        first, last = self.view["audit"]["first"], self.view["audit"]["last"]

        for lane in self.view["audit"]["lanes"]:
            for event in lane["events"]:
                self.assertGreaterEqual(event["seq"], first)
                self.assertLessEqual(event["seq"], last)

    def test_a_superseded_version_contributes_nothing(self) -> None:
        """This is GATE-VER-001 as something a reader can click."""

        superseded = [
            version
            for version in self.view["lineage"]["versions"]
            if version["superseded"]
        ]

        self.assertTrue(superseded, "the P0 scenario replaces versions on purpose")
        for version in superseded:
            self.assertEqual(
                version["field_count"],
                0,
                f'{version["version_id"]} was replaced but still reaches the final',
            )
            self.assertFalse(version["contributed"])

    def test_field_counts_sum_to_the_lineage_rows(self) -> None:
        by_version = sum(
            version["field_count"] for version in self.view["lineage"]["versions"]
        )

        self.assertEqual(by_version, len(self.view["lineage"]["fields"]))

    def test_a_run_without_model_calls_reports_zero_not_missing(self) -> None:
        """The deterministic evaluation spends nothing; that is its design."""

        summary = self.view["tokens"]["summary"]

        self.assertEqual(summary["calls"], 0)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertIn("note", summary)

    def test_runs_are_listed_for_the_left_rail(self) -> None:
        runs = self.view["runs"]

        self.assertTrue(runs)
        self.assertIn("run_p0", [run["run_id"] for run in runs])
        for run in runs:
            self.assertIn("events", run)


if __name__ == "__main__":
    unittest.main()
