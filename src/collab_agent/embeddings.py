"""Semantic similarity for cross-meeting linkage.

Why this exists, given the project already refused an embedding index
--------------------------------------------------------------------
Scale is not the reason. Eighteen action items fit in a prompt, and building a
vector store for them would be the kind of speculative infrastructure this
project already deleted once. The reason is **recall**.

Two real meetings forty-two minutes apart contain these pairs:

    研究热点风格与抖音指数      ←  调研并掌握抖音指数工具使用方法      字面 0.385
    拆解素材并撰写生活攻略脚本  ←  围绕《悉尼留学生活指南》撰写攻略类视频脚本  字面 0.353

Any person reads those as the same work. `DeterministicLinker` scores them
below its 0.62 threshold and cannot see them at all. That gap is what an
embedding closes, and it is the gap the extraction tool-calling experiment did
not have -- there, the deterministic path was already at 1.0 and had nothing
left to recover.

It also passes the rule that experiment produced: an operation that can be
checked deterministically after the fact does not belong to a model. Whether
two differently worded tasks are the same work has no ground-truth string to
compare against, so it cannot be.

Why no pgvector
---------------
pgvector exists only on PostgreSQL, and this project's CI runs the whole suite
against SQLite and PostgreSQL to prove the two share one domain semantics.
Buying a vector index would spend that. Vectors are stored as JSON and scored
in Python, which at this size is microseconds; `rank_candidates` is the single
seam where an index would go if the corpus ever outgrew it.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from typing import Any, Callable, Sequence

from .models import stable_hash


DEFAULT_EMBEDDING_ENDPOINT = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
)
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"

# Measured on the two 2026-03-02 meetings with text-embedding-v3, not guessed:
#
#   0.760  研究热点风格与抖音指数      ←  调研并掌握抖音指数工具使用方法
#   0.706  拆解素材并撰写生活攻略脚本  ←  围绕《悉尼留学生活指南》撰写攻略类视频脚本
#   0.554  制定调研与挖掘大纲          ←  调研抖音底层算法和推流机制      (weak)
#   0.393  建立四人迎新项目协作群      ←  调研抖音底层算法和推流机制      (unrelated)
#
# Real continuations land at 0.71-0.76 and unrelated work at 0.39, with a clean
# gap between. 0.65 sits in that gap. Nothing filters on it today -- the pool is
# passed whole -- so it only marks which candidates a retrieval cut would keep
# once a pool stops fitting in a prompt. Re-measure before trusting it with a
# different embedding model.
SEMANTIC_LINK_THRESHOLD = 0.65

EMBEDDING_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_sim_time TEXT NOT NULL,
    PRIMARY KEY (content_hash, model)
)
"""


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be obtained."""


def ensure_schema(database: Any) -> None:
    with database.transaction() as cursor:
        cursor.execute(EMBEDDING_CACHE_DDL)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, clamped to [-1, 1] against floating point drift."""

    if not left or not right or len(left) != len(right):
        raise ValueError("cosine needs two vectors of equal, non-zero length")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def lexical_similarity(left: str, right: str) -> float:
    """The same measure `DeterministicLinker` uses, exposed for comparison.

    Reported alongside the semantic score so the difference between the two is
    visible rather than asserted.
    """

    return SequenceMatcher(None, str(left or ""), str(right or "")).ratio()


class BailianEmbedder:
    """Embeddings from Bailian's OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: int = 60,
        batch_size: int = 10,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise EmbeddingError("DASHSCOPE_API_KEY is not configured")
        self.model = model or os.getenv("BAILIAN_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        self.endpoint = endpoint or os.getenv(
            "DASHSCOPE_EMBEDDINGS_URL", DEFAULT_EMBEDDING_ENDPOINT
        )
        self.timeout_seconds = timeout_seconds
        self.batch_size = max(1, int(batch_size))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(
                {"model": self.model, "input": batch}, ensure_ascii=False
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "colwork-agent-p0/0.1",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise EmbeddingError(
                f"embedding request returned HTTP {error.code}: {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise EmbeddingError(f"embedding request failed: {error}") from error

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError(
                f"embedding response held {len(data or [])} vectors for "
                f"{len(batch)} inputs"
            )
        # The API is documented to echo the input order, but an index field is
        # provided; sorting on it makes the mapping explicit rather than
        # assumed, because a silent misalignment would attach one task's
        # meaning to another's text.
        ordered = sorted(data, key=lambda row: int(row.get("index", 0)))
        return [[float(value) for value in row["embedding"]] for row in ordered]


class CachedEmbedder:
    """Wraps an embedder with a durable cache keyed on content and model.

    An embedding is a pure function of (model, text), so a cache hit is exactly
    as good as a call. Meeting titles barely change between runs, which makes a
    repeated demo essentially free after the first pass -- and a demo that does
    not re-embed is a demo that cannot fail on a rate limit.
    """

    def __init__(
        self,
        database: Any,
        embedder: Any,
        *,
        sim_time: str,
        model: str | None = None,
    ) -> None:
        self.database = database
        self.embedder = embedder
        self.sim_time = sim_time
        self.model = model or getattr(embedder, "model", DEFAULT_EMBEDDING_MODEL)
        self.hits = 0
        self.misses = 0
        ensure_schema(database)

    def _key(self, text: str) -> str:
        return stable_hash({"text": text, "model": self.model})

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [self._key(text) for text in texts]
        found: dict[str, list[float]] = {}
        for key in set(keys):
            row = self.database.one(
                "SELECT vector FROM embedding_cache WHERE content_hash = ? "
                "AND model = ?",
                (key, self.model),
            )
            if row:
                found[key] = json.loads(dict(row)["vector"])

        missing = [
            (key, text)
            for key, text in zip(keys, texts)
            if key not in found
        ]
        # De-duplicate before calling: the same title can appear twice in one
        # ranking pass and there is no reason to pay for it twice.
        unique_missing: dict[str, str] = {}
        for key, text in missing:
            unique_missing.setdefault(key, text)

        if unique_missing:
            order = list(unique_missing)
            vectors = self.embedder.embed([unique_missing[key] for key in order])
            with self.database.transaction() as cursor:
                for key, vector in zip(order, vectors):
                    found[key] = vector
                    cursor.execute(
                        "INSERT INTO embedding_cache("
                        "content_hash, model, vector, dimensions, created_sim_time"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (
                            key,
                            self.model,
                            json.dumps(vector),
                            len(vector),
                            self.sim_time,
                        ),
                    )
        self.misses += len(unique_missing)
        self.hits += len(texts) - len(unique_missing)
        return [found[key] for key in keys]


def candidate_text(item: dict[str, Any]) -> str:
    """What gets embedded for one action item.

    Title and deliverable together, because a title alone is often too short to
    carry meaning ("列大纲") while the deliverable says what it produces.
    """

    title = str(item.get("title") or "").strip()
    deliverable = str(item.get("deliverable_key") or item.get("deliverable") or "").strip()
    return f"{title}｜{deliverable}" if deliverable else title


def rank_candidates(
    item: dict[str, Any],
    pool: list[dict[str, Any]],
    *,
    embed: Callable[[Sequence[str]], list[list[float]]] | None = None,
) -> list[dict[str, Any]]:
    """Score every candidate lexically, and semantically when an embedder is given.

    Returns the pool ordered by the best available score, each row carrying
    both numbers so the difference is inspectable. This is the seam an index
    would replace: today it scores the whole pool because the whole pool is
    small.
    """

    if not pool:
        return []
    lexical = [
        lexical_similarity(candidate_text(item), candidate_text(prior))
        for prior in pool
    ]
    semantic: list[float | None] = [None] * len(pool)

    if embed is not None:
        vectors = embed([candidate_text(item)] + [candidate_text(p) for p in pool])
        query, others = vectors[0], vectors[1:]
        semantic = [cosine(query, other) for other in others]

    ranked = [
        {
            **prior,
            "lexical_similarity": round(lexical[index], 4),
            "semantic_similarity": (
                round(semantic[index], 4) if semantic[index] is not None else None
            ),
            # What a retrieval cut would keep. Advisory only today, because
            # nothing is cut -- recorded so the eventual cut can be justified
            # against runs where the whole pool was visible.
            "above_retrieval_threshold": (
                semantic[index] >= SEMANTIC_LINK_THRESHOLD
                if semantic[index] is not None
                else None
            ),
        }
        for index, prior in enumerate(pool)
    ]
    ranked.sort(
        key=lambda row: (
            row["semantic_similarity"]
            if row["semantic_similarity"] is not None
            else row["lexical_similarity"]
        ),
        reverse=True,
    )
    return ranked
