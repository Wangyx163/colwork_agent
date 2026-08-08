"""Propose that a new action item continues an earlier one.

Why full context and not retrieval
----------------------------------
The obvious shape for this is retrieval: embed prior action items, search,
feed the top-k. At this project's volume that would be infrastructure with
nothing to measure. Three episodes hold eighteen action items; rendered for a
prompt the whole set is well under a thousand tokens, and the authorisation
filter below cuts it further -- a given person only ever sees the meetings
they attended. Retrieval becomes necessary somewhere north of a thousand
items, which is a hundred-plus meetings away.

So the candidate pool is passed whole. The part that is actually unknown is
whether a linkage proposal helps a coordinator at all; that is what this
tests. When volume outgrows the context window, `candidate_pool` is the single
place a retrieval step would slot in, and by then there would be data to
justify it.

Why a model at all
------------------
Deterministic matching catches a restated title and an identical
`deliverable_key`, and `DeterministicLinker` below is exactly that -- the
zero-model floor, on the same footing as the extraction evaluation's keyword
floor. What it cannot catch is "整理采访问题" continuing "汇总大家提的问题清单":
same work, no shared substring. Unlike a citation, a semantic continuation has
no ground truth string to check afterwards, so it cannot be resolved by
deterministic code after the fact. That is the test for whether a model earns
its place here, and this passes it where the extraction tools did not.

What the model may not do
-------------------------
Propose a link to an item that is not in the pool. Every returned id is
checked against the pool and an invented one is dropped, which is the same
grounding discipline `source_quote` gets. And a proposal is only ever a
proposal: `PROPOSED` rows change no task state, and only a person moves one to
`CONFIRMED`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable
from uuid import uuid4

from .models import canonical_json


LINK_RELATIONS = ("CONTINUATION", "DUPLICATE")
LINK_STATUSES = ("PROPOSED", "CONFIRMED", "REJECTED")

# Deterministic floor thresholds. Deliberately high: the floor exists to be a
# floor, so a near-miss should be left for the model rather than claimed here.
DUPLICATE_TITLE_RATIO = 0.86
CONTINUATION_TITLE_RATIO = 0.62

LINKAGE_PROMPT_VERSION = "action-item-linkage.v1.0"

ACTION_ITEM_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS action_item_links (
    link_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    prior_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    relation TEXT NOT NULL CHECK (relation IN ('CONTINUATION', 'DUPLICATE')),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')),
    source TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL,
    proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    proposed_sim_time TEXT NOT NULL,
    decided_by_actor_id TEXT REFERENCES actors(actor_id),
    decided_sim_time TEXT,
    CHECK (action_item_id <> prior_action_item_id),
    UNIQUE(action_item_id, prior_action_item_id)
)
"""

# Same organisation, a different episode, and the person asking was on that
# episode's roster. The roster is this project's authorisation boundary, so
# crossing an episode must not be a way around it: being in this meeting does
# not entitle anyone to see one they were not in.
#
# Ordering is by `created_sim_time`, which records when an episode was loaded,
# not when the meeting happened -- the meeting date is not persisted on the
# episode. So this returns *other* meetings, not provably *earlier* ones, and
# a meeting imported out of order will appear out of order. The direction of a
# CONTINUATION therefore rests on what the model reads in the two titles, not
# on this ordering. Persisting a meeting date would let the query enforce it;
# until then the honest claim is "other meetings you attended".
CANDIDATE_POOL_SQL = """
SELECT a.action_item_id, a.title, a.deliverable_key, a.identity_key,
       a.status, a.episode_id, e.created_sim_time AS episode_created_sim_time
FROM action_items a
JOIN episodes e ON e.episode_id = a.episode_id
WHERE e.organization_id = (
        SELECT organization_id FROM episodes WHERE episode_id = ?
      )
  AND a.episode_id <> ?
  AND EXISTS (
        SELECT 1 FROM episode_participants p
        WHERE p.episode_id = a.episode_id AND p.actor_id = ?
      )
ORDER BY e.created_sim_time DESC, a.action_item_id
"""


class LinkageError(RuntimeError):
    """Raised when a linkage proposal cannot be trusted or stored."""


@dataclass(frozen=True)
class LinkProposal:
    prior_action_item_id: str
    relation: str
    reason: str
    confidence: float
    source: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "prior_action_item_id": self.prior_action_item_id,
            "relation": self.relation,
            "reason": self.reason,
            "confidence": self.confidence,
            "source": self.source,
        }


def ensure_schema(database: Any) -> None:
    with database.transaction() as cursor:
        cursor.execute(ACTION_ITEM_LINKS_DDL)


def candidate_pool(
    database: Any, *, episode_id: str, actor_id: str
) -> list[dict[str, Any]]:
    """Action items from this actor's other meetings in the same organisation.

    "Other", not "earlier" -- see the note on CANDIDATE_POOL_SQL. Run linkage
    from the later meeting so a recorded `prior_action_item_id` really is the
    earlier one.
    """

    rows = database.all(CANDIDATE_POOL_SQL, (episode_id, episode_id, actor_id))
    return [dict(row) for row in rows]


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, str(left or ""), str(right or "")).ratio()


class DeterministicLinker:
    """The zero-model floor: identical keys and near-identical titles.

    Anything this finds costs nothing, so a model that cannot beat it is not
    earning its tokens -- the same standard the extraction evaluation holds its
    keyword floor to.
    """

    def propose(
        self, item: dict[str, Any], pool: list[dict[str, Any]]
    ) -> list[LinkProposal]:
        proposals: list[LinkProposal] = []
        title = str(item.get("title") or "")
        identity = str(item.get("identity_key") or "")
        deliverable = str(item.get("deliverable_key") or "")
        for prior in pool:
            prior_title = str(prior.get("title") or "")
            ratio = _ratio(title, prior_title)
            if identity and identity == str(prior.get("identity_key") or ""):
                proposals.append(
                    LinkProposal(
                        prior_action_item_id=str(prior["action_item_id"]),
                        relation="DUPLICATE",
                        reason="identity_key 相同",
                        confidence=1.0,
                        source="DETERMINISTIC",
                    )
                )
                continue
            if ratio >= DUPLICATE_TITLE_RATIO:
                proposals.append(
                    LinkProposal(
                        prior_action_item_id=str(prior["action_item_id"]),
                        relation="DUPLICATE",
                        reason=f"标题高度相似（{ratio:.2f}）",
                        confidence=round(ratio, 3),
                        source="DETERMINISTIC",
                    )
                )
                continue
            if (
                deliverable
                and deliverable == str(prior.get("deliverable_key") or "")
                and ratio >= CONTINUATION_TITLE_RATIO
            ):
                proposals.append(
                    LinkProposal(
                        prior_action_item_id=str(prior["action_item_id"]),
                        relation="CONTINUATION",
                        reason=f"交付物相同且标题相近（{ratio:.2f}）",
                        confidence=round(ratio, 3),
                        source="DETERMINISTIC",
                    )
                )
        return proposals


def _render_pool(pool: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'- id={prior["action_item_id"]} | 会议={prior["episode_id"]} '
        f'| 标题={prior["title"]} | 交付物={prior["deliverable_key"]} '
        f'| 状态={prior["status"]}'
        for prior in pool
    )


def linkage_messages(
    item: dict[str, Any], pool: list[dict[str, Any]]
) -> list[dict[str, str]]:
    system = """你判断一条新的会议行动项是不是在延续以往会议中的某一条。

规则：
1. 只输出 JSON，不要 Markdown。
2. prior_action_item_id 必须逐字来自候选清单里的 id，不得编造、不得改写。
3. 没有把握就不要给出关联。宁可漏，不可错——错误关联会让负责人把两件无关的事并成一件。
4. relation 二选一：
   - DUPLICATE：同一件事被重新提出，工作内容基本重合。
   - CONTINUATION：新任务是旧任务的下一步或后续阶段，不是同一件事。
5. 只是同一个主题、同一个人负责、或同属一个项目，都不足以构成关联；
   必须是同一件工作的重述或直接后续。
6. reason 用一句话说明依据，引用两边的具体措辞。

返回：
{"links": [{"prior_action_item_id": "...", "relation": "DUPLICATE 或 CONTINUATION",
            "reason": "一句话依据", "confidence": 0.0}]}
没有关联时返回 {"links": []}。"""
    user = (
        f'新行动项：\n标题={item.get("title")}\n'
        f'交付物={item.get("deliverable_key")}\n\n'
        f"以往会议的候选行动项（只能从这里选）：\n{_render_pool(pool)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class LinkageProposer:
    """Model-backed linkage, with every returned id checked against the pool."""

    def __init__(self, complete: Callable[[list[dict[str, str]]], str]) -> None:
        self.complete = complete

    def propose(
        self, item: dict[str, Any], pool: list[dict[str, Any]]
    ) -> list[LinkProposal]:
        if not pool:
            return []
        raw = self.complete(linkage_messages(item, pool))
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise LinkageError(f"linkage model returned non-JSON: {raw!r}") from error
        if not isinstance(payload, dict):
            raise LinkageError("linkage model returned a non-object payload")

        allowed = {str(prior["action_item_id"]) for prior in pool}
        proposals: list[LinkProposal] = []
        seen: set[str] = set()
        for entry in payload.get("links") or []:
            if not isinstance(entry, dict):
                continue
            prior_id = str(entry.get("prior_action_item_id") or "")
            # An id outside the pool is either a hallucination or a leak past
            # the authorisation filter. Either way it is dropped, not repaired.
            if prior_id not in allowed or prior_id in seen:
                continue
            relation = str(entry.get("relation") or "").strip().upper()
            if relation not in LINK_RELATIONS:
                continue
            try:
                confidence = float(entry.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            seen.add(prior_id)
            proposals.append(
                LinkProposal(
                    prior_action_item_id=prior_id,
                    relation=relation,
                    reason=str(entry.get("reason") or "").strip(),
                    confidence=max(0.0, min(1.0, confidence)),
                    source="MODEL",
                )
            )
        return proposals


def bailian_completer(
    *, model: str | None = None
) -> Callable[[list[dict[str, str]]], str]:
    """Back the proposer with Bailian, reusing the extractor's request layer.

    Imported lazily so the linkage module keeps working offline -- and so the
    tests, which inject their own callable, never need credentials.
    """

    from .extraction import BailianExtractor

    return BailianExtractor(model=model).complete_json


def propose_for_action_item(
    database: Any,
    *,
    run_id: str,
    episode_id: str,
    action_item: dict[str, Any],
    actor_id: str,
    sim_time: str,
    complete: Callable[[list[dict[str, str]]], str] | None = None,
    embed: Callable[[Any], list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Run both linkers over one action item and store what they propose.

    The deterministic floor always runs; the model runs only when one is
    supplied. Their proposals are merged with the floor winning any pair they
    both name, because a free certain answer beats a billed probable one.

    `embed` adds a semantic score to every candidate. It changes what the
    result reports, not what it decides -- ranking a pool that is passed whole
    cannot alter the model's input, so the scores are there to be inspected and
    to mark where a retrieval cut would go once the pool outgrows a prompt.
    """

    pool = candidate_pool(database, episode_id=episode_id, actor_id=actor_id)
    if not pool:
        return {"pool_size": 0, "stored": [], "proposals": [], "ranked": []}

    ranked: list[dict[str, Any]] = []
    if embed is not None:
        from .embeddings import rank_candidates

        ranked = rank_candidates(action_item, pool, embed=embed)

    proposals = DeterministicLinker().propose(action_item, pool)
    claimed = {proposal.prior_action_item_id for proposal in proposals}
    if complete is not None:
        for proposal in LinkageProposer(complete).propose(action_item, pool):
            if proposal.prior_action_item_id not in claimed:
                proposals.append(proposal)

    stored = record_proposals(
        database,
        run_id=run_id,
        episode_id=episode_id,
        action_item_id=str(action_item["action_item_id"]),
        proposals=proposals,
        proposed_by_actor_id=actor_id,
        sim_time=sim_time,
    )
    return {
        "pool_size": len(pool),
        "stored": stored,
        "proposals": [proposal.as_payload() for proposal in proposals],
        "ranked": ranked,
    }


def record_proposals(
    database: Any,
    *,
    run_id: str,
    episode_id: str,
    action_item_id: str,
    proposals: list[LinkProposal],
    proposed_by_actor_id: str,
    sim_time: str,
) -> list[str]:
    """Store proposals, skipping any pair that already has a row.

    The UNIQUE pair is what makes a repeated proposal a no-op: re-running the
    proposer must not resurrect a link a person already rejected.
    """

    stored: list[str] = []
    with database.transaction() as cursor:
        for proposal in proposals:
            existing = cursor.execute(
                "SELECT link_id FROM action_item_links "
                "WHERE action_item_id = ? AND prior_action_item_id = ?",
                (action_item_id, proposal.prior_action_item_id),
            ).fetchone()
            if existing:
                continue
            link_id = f"lnk_{uuid4().hex}"
            cursor.execute(
                """
                INSERT INTO action_item_links(
                    link_id, episode_id, action_item_id, prior_action_item_id,
                    relation, status, source, reason, confidence,
                    proposed_by_actor_id, proposed_sim_time
                ) VALUES (?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    episode_id,
                    action_item_id,
                    proposal.prior_action_item_id,
                    proposal.relation,
                    proposal.source,
                    proposal.reason,
                    proposal.confidence,
                    proposed_by_actor_id,
                    sim_time,
                ),
            )
            stored.append(link_id)
        if stored:
            database.append_audit(
                cursor,
                run_id=run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemLinksProposed",
                sim_time=sim_time,
                payload={
                    "link_ids": stored,
                    "proposals": [p.as_payload() for p in proposals],
                    "prompt_version": LINKAGE_PROMPT_VERSION,
                },
                correlation_id=f"corr_linkage_{action_item_id}",
            )
    return stored


def decide_link(
    database: Any,
    *,
    run_id: str,
    link_id: str,
    approve: bool,
    actor_id: str,
    sim_time: str,
) -> dict[str, Any]:
    """Confirm or reject one proposed link. Only a person calls this."""

    with database.transaction() as cursor:
        row = cursor.execute(
            "SELECT * FROM action_item_links WHERE link_id = ?", (link_id,)
        ).fetchone()
        if not row:
            raise LinkageError(f"no such link {link_id}")
        record = dict(row)
        if record["status"] != "PROPOSED":
            # Already decided. Replaying the same decision is fine; changing it
            # is not, because downstream may have read the earlier outcome.
            return {
                "link_id": link_id,
                "status": record["status"],
                "already_decided": True,
            }
        status = "CONFIRMED" if approve else "REJECTED"
        cursor.execute(
            "UPDATE action_item_links SET status = ?, decided_by_actor_id = ?, "
            "decided_sim_time = ? WHERE link_id = ?",
            (status, actor_id, sim_time, link_id),
        )
        database.append_audit(
            cursor,
            run_id=run_id,
            aggregate_type="ActionItem",
            aggregate_id=record["action_item_id"],
            event_type="ActionItemLinkDecided",
            sim_time=sim_time,
            payload={
                "link_id": link_id,
                "prior_action_item_id": record["prior_action_item_id"],
                "relation": record["relation"],
                "status": status,
                "decided_by": actor_id,
            },
            correlation_id=f'corr_linkage_{record["action_item_id"]}',
        )
    return {"link_id": link_id, "status": status, "already_decided": False}


def links_for(
    database: Any, *, action_item_id: str, statuses: tuple[str, ...] = LINK_STATUSES
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in statuses)
    rows = database.all(
        f"SELECT * FROM action_item_links WHERE action_item_id = ? "
        f"AND status IN ({placeholders}) ORDER BY proposed_sim_time, link_id",
        (action_item_id, *statuses),
    )
    return [dict(row) for row in rows]


def linkage_summary(database: Any, *, episode_id: str) -> dict[str, Any]:
    """Counts for the diagnostics page and the product evaluation."""

    rows = database.all(
        "SELECT status, source, count(*) AS n FROM action_item_links "
        "WHERE episode_id = ? GROUP BY status, source",
        (episode_id,),
    )
    counts: dict[str, int] = {}
    by_source: dict[str, dict[str, int]] = {}
    for row in rows:
        record = dict(row)
        counts[record["status"]] = counts.get(record["status"], 0) + int(record["n"])
        by_source.setdefault(record["source"], {})[record["status"]] = int(record["n"])
    return {"by_status": counts, "by_source": by_source}
