from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.embeddings import (
    SEMANTIC_LINK_THRESHOLD,
    CachedEmbedder,
    candidate_text,
    cosine,
    ensure_schema,
    lexical_similarity,
    rank_candidates,
)
from collab_agent.store import Database


SIM_TIME = "2026-03-09T09:00:00+08:00"

# The real pair from the two 2026-03-02 meetings, and what each measure sees.
NEW_ITEM = {"title": "研究热点风格与抖音指数", "deliverable_key": "热点风格与指数研究"}
RELATED = {
    "action_item_id": "ai_related",
    "title": "调研并掌握抖音指数工具使用方法",
    "deliverable_key": "抖音指数工具掌握",
}
UNRELATED = {
    "action_item_id": "ai_unrelated",
    "title": "建立四人迎新项目协作群",
    "deliverable_key": "协作群",
}


class FakeEmbedder:
    """Deterministic stand-in: related texts get near-parallel vectors."""

    model = "fake-embedding"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            # 抖音 pulls one axis, 群 another, so the fixture encodes the same
            # relatedness a real model would find without needing the network.
            vectors.append(
                [
                    1.0 if "抖音" in text else 0.0,
                    1.0 if "指数" in text else 0.0,
                    1.0 if "群" in text else 0.0,
                    0.1,
                ]
            )
        return vectors


class CosineTests(unittest.TestCase):
    def test_identical_vectors_score_one(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 2.0], [1.0, 2.0]), 1.0, places=6)

    def test_orthogonal_vectors_score_zero(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_a_zero_vector_scores_zero_rather_than_dividing(self) -> None:
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_mismatched_lengths_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            cosine([1.0], [1.0, 2.0])


class GapTests(unittest.TestCase):
    """The premise: lexical similarity cannot see this pair."""

    def test_the_real_pair_scores_below_the_deterministic_threshold(self) -> None:
        score = lexical_similarity(
            "研究热点风格与抖音指数", "调研并掌握抖音指数工具使用方法"
        )

        self.assertLess(
            score,
            0.62,
            "if lexical similarity could see this, the embedding would be "
            "unjustified infrastructure",
        )

    def test_semantic_similarity_recovers_it(self) -> None:
        ranked = rank_candidates(
            NEW_ITEM, [RELATED, UNRELATED], embed=FakeEmbedder().embed
        )

        top = ranked[0]
        self.assertEqual(top["action_item_id"], "ai_related")
        self.assertGreater(top["semantic_similarity"], top["lexical_similarity"])


class ThresholdTests(unittest.TestCase):
    """The threshold came from measurement; keep it inside the measured gap."""

    def test_the_threshold_sits_between_related_and_unrelated(self) -> None:
        related_low, unrelated_high = 0.706, 0.393

        self.assertLess(SEMANTIC_LINK_THRESHOLD, related_low)
        self.assertGreater(SEMANTIC_LINK_THRESHOLD, unrelated_high)

    def test_the_flag_follows_the_threshold(self) -> None:
        ranked = rank_candidates(
            NEW_ITEM, [RELATED, UNRELATED], embed=FakeEmbedder().embed
        )

        for row in ranked:
            self.assertEqual(
                row["above_retrieval_threshold"],
                row["semantic_similarity"] >= SEMANTIC_LINK_THRESHOLD,
            )

    def test_no_flag_without_a_semantic_score(self) -> None:
        ranked = rank_candidates(NEW_ITEM, [RELATED])

        self.assertIsNone(ranked[0]["above_retrieval_threshold"])


class RankingTests(unittest.TestCase):
    def test_both_scores_are_reported_for_every_candidate(self) -> None:
        ranked = rank_candidates(
            NEW_ITEM, [RELATED, UNRELATED], embed=FakeEmbedder().embed
        )

        for row in ranked:
            self.assertIsNotNone(row["lexical_similarity"])
            self.assertIsNotNone(row["semantic_similarity"])

    def test_without_an_embedder_it_falls_back_to_lexical_only(self) -> None:
        ranked = rank_candidates(NEW_ITEM, [RELATED, UNRELATED])

        self.assertTrue(all(row["semantic_similarity"] is None for row in ranked))
        self.assertEqual(
            [row["action_item_id"] for row in ranked],
            ["ai_related", "ai_unrelated"],
            "lexical still orders them, just less confidently",
        )

    def test_an_empty_pool_costs_no_embedding_call(self) -> None:
        embedder = FakeEmbedder()

        self.assertEqual(rank_candidates(NEW_ITEM, [], embed=embedder.embed), [])
        self.assertEqual(embedder.calls, [])

    def test_candidate_text_joins_title_and_deliverable(self) -> None:
        self.assertEqual(candidate_text(NEW_ITEM), "研究热点风格与抖音指数｜热点风格与指数研究")
        self.assertEqual(candidate_text({"title": "只有标题"}), "只有标题")


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "emb.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        ensure_schema(self.database)
        self.embedder = FakeEmbedder()
        self.cached = CachedEmbedder(
            self.database, self.embedder, sim_time=SIM_TIME
        )

    def test_a_second_pass_calls_nothing(self) -> None:
        first = self.cached.embed(["甲", "乙"])
        calls_after_first = len(self.embedder.calls)

        second = self.cached.embed(["甲", "乙"])

        self.assertEqual(first, second)
        self.assertEqual(len(self.embedder.calls), calls_after_first)
        self.assertEqual(self.cached.hits, 2)

    def test_a_repeated_text_in_one_pass_is_embedded_once(self) -> None:
        self.cached.embed(["同一句", "同一句", "另一句"])

        self.assertEqual(
            sorted(self.embedder.calls[0]),
            sorted(["同一句", "另一句"]),
            "paying twice for the same string in one batch is waste",
        )

    def test_only_the_missing_texts_are_sent(self) -> None:
        self.cached.embed(["甲"])
        self.embedder.calls.clear()

        self.cached.embed(["甲", "丙"])

        self.assertEqual(self.embedder.calls, [["丙"]])

    def test_the_cache_survives_a_new_wrapper(self) -> None:
        self.cached.embed(["甲"])
        fresh_embedder = FakeEmbedder()

        CachedEmbedder(
            self.database, fresh_embedder, sim_time=SIM_TIME
        ).embed(["甲"])

        self.assertEqual(fresh_embedder.calls, [], "a restart must not re-embed")

    def test_a_different_model_does_not_reuse_another_model_vectors(self) -> None:
        self.cached.embed(["甲"])
        other = FakeEmbedder()
        other.model = "other-embedding"

        CachedEmbedder(self.database, other, sim_time=SIM_TIME).embed(["甲"])

        self.assertEqual(other.calls, [["甲"]])

    def test_vectors_are_stored_as_portable_json(self) -> None:
        """No pgvector: the same rows must load on SQLite and PostgreSQL."""

        self.cached.embed(["甲"])

        row = dict(self.database.one("SELECT vector, dimensions FROM embedding_cache"))
        self.assertEqual(len(json.loads(row["vector"])), row["dimensions"])


if __name__ == "__main__":
    unittest.main()
