from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.linkage import (
    DeterministicLinker,
    LinkageError,
    LinkageProposer,
    LinkProposal,
    candidate_pool,
    decide_link,
    ensure_schema,
    linkage_messages,
    linkage_summary,
    links_for,
    propose_for_action_item,
    record_proposals,
    resolve_link_id,
)
from collab_agent.store import Database


SIM_TIME = "2026-03-09T09:00:00+08:00"


def _seed(database: Database) -> dict[str, str]:
    """Two organisations, three meetings, deliberately overlapping rosters.

    甲 attends both ACISC meetings. 乙 attends only the newer one. 丙 belongs to
    a different organisation entirely. That is enough to catch both ways the
    pool can leak.
    """

    with database.transaction() as cursor:
        for org in ("org_acisc", "org_other"):
            cursor.execute(
                "INSERT INTO organizations(organization_id, name, status, created_at) "
                "VALUES (?, ?, 'ACTIVE', ?)",
                (org, org, SIM_TIME),
            )
        actors = {
            "jia": "org_acisc",
            "yi": "org_acisc",
            "bing": "org_other",
        }
        for actor, org in actors.items():
            cursor.execute(
                "INSERT INTO actors(actor_id, organization_id, display_name, "
                "actor_type, status) VALUES (?, ?, ?, 'HUMAN', 'ACTIVE')",
                (actor, org, actor),
            )
        episodes = [
            ("ep_old", "org_acisc", "2026-03-01T09:00:00+08:00"),
            ("ep_new", "org_acisc", "2026-03-09T09:00:00+08:00"),
            ("ep_other", "org_other", "2026-03-05T09:00:00+08:00"),
        ]
        for episode_id, org, created in episodes:
            cursor.execute(
                """
                INSERT INTO episodes(
                    episode_id, organization_id, run_id, content_pack_id,
                    owner_actor_id, status, transcript, current_sim_time,
                    created_sim_time, evaluation_cutoff_sim_time
                ) VALUES (?, ?, 'run', 'pack', ?, 'ACTIVE', '', ?, ?, ?)
                """,
                (
                    episode_id,
                    org,
                    "jia" if org == "org_acisc" else "bing",
                    created,
                    created,
                    created,
                ),
            )
        rosters = [
            ("ep_old", "jia"),
            ("ep_new", "jia"),
            ("ep_new", "yi"),
            ("ep_other", "bing"),
        ]
        for episode_id, actor in rosters:
            cursor.execute(
                "INSERT INTO episode_participants(episode_id, actor_id, role) "
                "VALUES (?, ?, 'PARTICIPANT')",
                (episode_id, actor),
            )
        items = [
            ("ai_old_questions", "ep_old", "整理采访问题清单", "采访问题清单", "k_q"),
            ("ai_old_venue", "ep_old", "联系拍摄场地", "场地确认", "k_v"),
            ("ai_other_secret", "ep_other", "另一个组织的机密任务", "机密交付", "k_s"),
            ("ai_new", "ep_new", "汇总大家提的问题清单", "采访问题清单", "k_n"),
        ]
        for action_id, episode_id, title, deliverable, identity in items:
            cursor.execute(
                """
                INSERT INTO action_items(
                    action_item_id, episode_id, identity_key, title,
                    deliverable_key, required, status, sla_id,
                    source_message_id, source_span, created_sim_time
                ) VALUES (?, ?, ?, ?, ?, 1, 'PENDING_CONFIRMATION', 'sla',
                          'msg', '{}', ?)
                """,
                (action_id, episode_id, identity, title, deliverable, SIM_TIME),
            )
    return {"new": "ai_new"}


class LinkageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "link.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        ensure_schema(self.database)
        _seed(self.database)
        self.new_item = dict(
            self.database.one(
                "SELECT * FROM action_items WHERE action_item_id = ?", ("ai_new",)
            )
        )


class CandidatePoolAuthorizationTests(LinkageTestCase):
    """The roster is the authorisation boundary; crossing meetings must obey it."""

    def test_pool_holds_prior_meetings_the_actor_attended(self) -> None:
        pool = candidate_pool(self.database, episode_id="ep_new", actor_id="jia")

        self.assertEqual(
            sorted(row["action_item_id"] for row in pool),
            ["ai_old_questions", "ai_old_venue"],
        )

    def test_a_meeting_the_actor_missed_is_not_in_the_pool(self) -> None:
        """乙 is in this meeting but was not in the earlier one."""

        pool = candidate_pool(self.database, episode_id="ep_new", actor_id="yi")

        self.assertEqual(pool, [], "being in this meeting is not entitlement to a prior one")

    def test_another_organisation_never_appears(self) -> None:
        for actor in ("jia", "yi"):
            pool = candidate_pool(self.database, episode_id="ep_new", actor_id=actor)
            self.assertNotIn(
                "ai_other_secret",
                [row["action_item_id"] for row in pool],
                f"{actor} must never see another organisation's meeting",
            )

    def test_the_current_episode_is_excluded(self) -> None:
        pool = candidate_pool(self.database, episode_id="ep_new", actor_id="jia")

        self.assertNotIn("ai_new", [row["action_item_id"] for row in pool])


class DeterministicFloorTests(LinkageTestCase):
    def test_identical_identity_key_is_a_duplicate(self) -> None:
        pool = [
            {
                "action_item_id": "ai_old_questions",
                "title": "完全不同的标题",
                "deliverable_key": "别的",
                "identity_key": "k_n",
            }
        ]

        proposals = DeterministicLinker().propose(self.new_item, pool)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].relation, "DUPLICATE")
        self.assertEqual(proposals[0].source, "DETERMINISTIC")

    def test_shared_deliverable_with_similar_title_is_a_continuation(self) -> None:
        # 0.750 against the new item's title: above the continuation threshold,
        # below the duplicate one, which is the band this branch exists for.
        pool = [
            {
                "action_item_id": "ai_old_questions",
                "title": "汇总问题清单",
                "deliverable_key": "采访问题清单",
                "identity_key": "k_q",
            }
        ]

        proposals = DeterministicLinker().propose(self.new_item, pool)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].relation, "CONTINUATION")

    def test_a_near_identical_title_is_a_duplicate_not_a_continuation(self) -> None:
        pool = [
            {
                "action_item_id": "ai_old_questions",
                "title": "汇总大家提的问题清单x",
                "deliverable_key": "采访问题清单",
                "identity_key": "k_q",
            }
        ]

        proposals = DeterministicLinker().propose(self.new_item, pool)

        self.assertEqual(proposals[0].relation, "DUPLICATE")

    def test_the_floor_misses_a_reworded_continuation(self) -> None:
        """This gap is the whole reason a model is involved at all."""

        pool = candidate_pool(self.database, episode_id="ep_new", actor_id="jia")

        proposals = DeterministicLinker().propose(self.new_item, pool)

        self.assertEqual(
            [p.prior_action_item_id for p in proposals],
            [],
            "整理采访问题清单 vs 汇总大家提的问题清单 shares no substring",
        )


class ModelProposerTests(LinkageTestCase):
    def _pool(self) -> list[dict]:
        return candidate_pool(self.database, episode_id="ep_new", actor_id="jia")

    def test_a_grounded_proposal_is_accepted(self) -> None:
        proposer = LinkageProposer(
            lambda _messages: json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_old_questions",
                            "relation": "CONTINUATION",
                            "reason": "同一份采访问题清单的后续汇总",
                            "confidence": 0.8,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

        proposals = proposer.propose(self.new_item, self._pool())

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].prior_action_item_id, "ai_old_questions")
        self.assertEqual(proposals[0].source, "MODEL")

    def test_an_invented_id_is_dropped(self) -> None:
        proposer = LinkageProposer(
            lambda _messages: json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_does_not_exist",
                            "relation": "DUPLICATE",
                            "reason": "编造的",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )

        self.assertEqual(proposer.propose(self.new_item, self._pool()), [])

    def test_an_id_outside_the_pool_is_dropped_even_though_it_exists(self) -> None:
        """A real id from another organisation must not become a link."""

        proposer = LinkageProposer(
            lambda _messages: json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_other_secret",
                            "relation": "DUPLICATE",
                            "reason": "越权",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )

        self.assertEqual(proposer.propose(self.new_item, self._pool()), [])

    def test_an_unknown_relation_is_dropped(self) -> None:
        proposer = LinkageProposer(
            lambda _messages: json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_old_questions",
                            "relation": "MAYBE_RELATED",
                            "reason": "?",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )

        self.assertEqual(proposer.propose(self.new_item, self._pool()), [])

    def test_non_json_is_an_error_not_a_silent_empty(self) -> None:
        proposer = LinkageProposer(lambda _messages: "我觉得它们有关系")

        with self.assertRaises(LinkageError):
            proposer.propose(self.new_item, self._pool())

    def test_an_empty_pool_costs_no_model_call(self) -> None:
        calls: list[int] = []

        def complete(_messages):
            calls.append(1)
            return "{}"

        LinkageProposer(complete).propose(self.new_item, [])

        self.assertEqual(calls, [], "nothing to link against means nothing to ask")

    def test_the_prompt_only_offers_pool_ids(self) -> None:
        messages = linkage_messages(self.new_item, self._pool())
        rendered = messages[-1]["content"]

        self.assertIn("ai_old_questions", rendered)
        self.assertNotIn("ai_other_secret", rendered)


class CombinedProposalTests(LinkageTestCase):
    def test_the_floor_wins_a_pair_both_linkers_name(self) -> None:
        """A free certain answer beats a billed probable one."""

        def complete(_messages):
            return json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_old_questions",
                            "relation": "DUPLICATE",
                            "reason": "模型认为重复",
                            "confidence": 0.7,
                        }
                    ]
                },
                ensure_ascii=False,
            )

        # Give the floor something to find on the same pair.
        with self.database.transaction() as cursor:
            cursor.execute(
                "UPDATE action_items SET identity_key = ? WHERE action_item_id = ?",
                ("k_n", "ai_old_questions"),
            )

        outcome = propose_for_action_item(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item=self.new_item,
            actor_id="jia",
            sim_time=SIM_TIME,
            complete=complete,
        )

        matching = [
            proposal
            for proposal in outcome["proposals"]
            if proposal["prior_action_item_id"] == "ai_old_questions"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["source"], "DETERMINISTIC")

    def test_the_model_adds_what_the_floor_cannot_see(self) -> None:
        def complete(_messages):
            return json.dumps(
                {
                    "links": [
                        {
                            "prior_action_item_id": "ai_old_questions",
                            "relation": "CONTINUATION",
                            "reason": "同一份清单的后续汇总",
                            "confidence": 0.8,
                        }
                    ]
                },
                ensure_ascii=False,
            )

        outcome = propose_for_action_item(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item=self.new_item,
            actor_id="jia",
            sim_time=SIM_TIME,
            complete=complete,
        )

        self.assertEqual(len(outcome["stored"]), 1)
        self.assertEqual(outcome["proposals"][0]["source"], "MODEL")

    def test_no_model_still_runs_the_floor(self) -> None:
        outcome = propose_for_action_item(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item=self.new_item,
            actor_id="jia",
            sim_time=SIM_TIME,
            complete=None,
        )

        self.assertEqual(outcome["pool_size"], 2)
        self.assertEqual(outcome["proposals"], [])

    def test_an_actor_with_no_history_short_circuits(self) -> None:
        outcome = propose_for_action_item(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item=self.new_item,
            actor_id="yi",
            sim_time=SIM_TIME,
            complete=lambda _m: self.fail("must not call the model"),
        )

        self.assertEqual(outcome["pool_size"], 0)
        self.assertEqual(outcome["stored"], [])


class StorageAndDecisionTests(LinkageTestCase):
    def _store(self) -> list[str]:
        return record_proposals(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item_id="ai_new",
            proposals=[
                LinkProposal(
                    prior_action_item_id="ai_old_questions",
                    relation="CONTINUATION",
                    reason="后续汇总",
                    confidence=0.8,
                    source="MODEL",
                )
            ],
            proposed_by_actor_id="jia",
            sim_time=SIM_TIME,
        )

    def test_a_proposal_changes_no_task_state(self) -> None:
        before = dict(
            self.database.one(
                "SELECT status, version FROM action_items WHERE action_item_id = ?",
                ("ai_new",),
            )
        )

        self._store()

        after = dict(
            self.database.one(
                "SELECT status, version FROM action_items WHERE action_item_id = ?",
                ("ai_new",),
            )
        )
        self.assertEqual(before, after)

    def test_re_proposing_the_same_pair_is_a_no_op(self) -> None:
        first = self._store()
        second = self._store()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(links_for(self.database, action_item_id="ai_new")), 1)

    def test_a_rejected_link_is_not_resurrected_by_re_proposing(self) -> None:
        link_id = self._store()[0]
        decide_link(
            self.database,
            run_id="run",
            link_id=link_id,
            approve=False,
            actor_id="jia",
            sim_time=SIM_TIME,
        )

        self._store()

        links = links_for(self.database, action_item_id="ai_new")
        self.assertEqual([row["status"] for row in links], ["REJECTED"])

    def test_confirming_records_who_decided(self) -> None:
        link_id = self._store()[0]

        outcome = decide_link(
            self.database,
            run_id="run",
            link_id=link_id,
            approve=True,
            actor_id="jia",
            sim_time=SIM_TIME,
        )

        self.assertEqual(outcome["status"], "CONFIRMED")
        self.assertFalse(outcome["already_decided"])
        row = links_for(self.database, action_item_id="ai_new")[0]
        self.assertEqual(row["decided_by_actor_id"], "jia")

    def test_deciding_twice_replays_rather_than_flipping(self) -> None:
        link_id = self._store()[0]
        decide_link(
            self.database,
            run_id="run",
            link_id=link_id,
            approve=True,
            actor_id="jia",
            sim_time=SIM_TIME,
        )

        repeat = decide_link(
            self.database,
            run_id="run",
            link_id=link_id,
            approve=False,
            actor_id="jia",
            sim_time=SIM_TIME,
        )

        self.assertTrue(repeat["already_decided"])
        self.assertEqual(repeat["status"], "CONFIRMED")

    def test_proposing_writes_an_audit_event(self) -> None:
        self._store()

        rows = self.database.all(
            "SELECT event_type FROM audit_events WHERE aggregate_id = ?", ("ai_new",)
        )
        self.assertIn("ActionItemLinksProposed", [dict(r)["event_type"] for r in rows])

    def test_a_unique_prefix_resolves_like_a_short_commit_id(self) -> None:
        link_id = self._store()[0]

        self.assertEqual(resolve_link_id(self.database, link_id[:10]), link_id)
        self.assertEqual(resolve_link_id(self.database, link_id), link_id)

    def test_an_ambiguous_prefix_asks_for_more_rather_than_guessing(self) -> None:
        self._store()
        record_proposals(
            self.database,
            run_id="run",
            episode_id="ep_new",
            action_item_id="ai_new",
            proposals=[
                LinkProposal(
                    prior_action_item_id="ai_old_venue",
                    relation="DUPLICATE",
                    reason="第二条",
                    confidence=0.5,
                    source="MODEL",
                )
            ],
            proposed_by_actor_id="jia",
            sim_time=SIM_TIME,
        )

        with self.assertRaises(LinkageError) as caught:
            resolve_link_id(self.database, "lnk")

        self.assertIn("2", str(caught.exception))

    def test_an_unknown_prefix_is_refused(self) -> None:
        self._store()

        with self.assertRaises(LinkageError):
            resolve_link_id(self.database, "lnk_nothing")

    def test_summary_counts_by_status_and_source(self) -> None:
        self._store()

        summary = linkage_summary(self.database, episode_id="ep_new")

        self.assertEqual(summary["by_status"]["PROPOSED"], 1)
        self.assertEqual(summary["by_source"]["MODEL"]["PROPOSED"], 1)


if __name__ == "__main__":
    unittest.main()
