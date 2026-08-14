"""Turn what a coordinator decided into item-level gold.

The evaluation has always had two numbers. `sentence_level_positive_f1` asks
whether the extractor pointed at the right lines; `item_level_detection` asks
whether it produced the right task list. The second is the system's actual job,
and it has been `null` in every report ever run, because the public corpus
labels sentences and nobody has ever written item gold for it.

So the project has been steering on the wrong instrument -- and it is the wrong
instrument in a specific way, not just a weaker one. The extractor emits items,
each citing one quote; a discussion that spans five sentences and produces one
task counts as five positives in the gold and one in the prediction. Precision
there is a function of how many lines you touch, not of how many tasks you got
right. With a positive rate under one percent, that difference is most of the
score.

Item gold is expensive to write, which is why nobody had. But it does not have
to be written: a coordinator reviewing extracted candidates is already
labelling them, one decision at a time, and the audit trail already records
every decision.

    dispatched or kept   -> the extractor was right about this one
    ignored              -> a false positive, said so by the person who would
                            have had to do the work
    added by hand        -> a false negative, in the coordinator's own words
    reworded             -> right about the task, wrong about how it reads

That is a confusion matrix produced as a by-product of using the product, and
it grows every time somebody runs a meeting. It is also better labelled than
anything an annotator could produce from a transcript alone: the person
deciding was in the room.

## What it does not solve

Volume. Three meetings is not a benchmark, whatever the labels cost. What this
changes is the marginal price of the fourth, which is zero.

## The honest caveat, carried into the file

These labels come from one team's judgement about their own meetings. That is
exactly right for measuring whether the extractor is useful *to them*, and
wrong for claiming a general result -- so every harvested file says so in a
field, rather than in a README nobody reads next to the number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


#: A coordinator's decision, and what it means as a label.
DECISION_LABELS = {
    "KEPT": "extraction proposed it and the coordinator kept it",
    "IGNORED": "extraction proposed it and the coordinator said it was not a task",
    "ADDED": "extraction missed it and the coordinator entered it by hand",
}


def harvest(database: Any, *, episode_id: str, run_id: str) -> dict[str, Any]:
    """Read one meeting's review decisions as item-level gold.

    Reads the action items rather than replaying the audit events: the items
    carry their final state and their source quote, and the audit is the record
    of how they got there. Both would give the same answer; the table gives it
    in one query and cannot drift out of order.
    """

    rows = database.all(
        "SELECT action_item_id, title, status, source_span, proposal_metadata "
        "FROM action_items WHERE episode_id = ? ORDER BY action_item_id",
        (episode_id,),
    )

    expected: list[dict[str, Any]] = []
    counts = {"kept": 0, "ignored": 0, "added": 0, "reworded": 0}
    for row in rows:
        item = dict(row)
        raw = item.get("proposal_metadata") or "{}"
        metadata = json.loads(raw) if isinstance(raw, str) else dict(raw)
        origin = metadata.get("origin")
        rejected = item["status"] == "REJECTED"

        if origin == "COORDINATOR_ADDED":
            counts["added"] += 1
            # A hand-entered task has no verified quote -- that is the whole
            # reason it is marked as hand-entered -- so it is gold for "this
            # should have been found", carrying the coordinator's note as the
            # only evidence there is.
            expected.append(
                {
                    "title": item["title"],
                    "source_quote": metadata.get("source_quote") or "",
                    "deliverable": metadata.get("deliverable") or "",
                    "owner_name": None,
                    "deadline_iso": None,
                    "label": "ADDED",
                    "grounded": False,
                }
            )
            continue

        if rejected:
            counts["ignored"] += 1
            # Recorded, not dropped. An extractor that stops proposing this one
            # has improved, and a gold file that only lists the good ones
            # cannot tell that from an extractor that found less of everything.
            continue

        counts["kept"] += 1
        if metadata.get("coordinator_reworded"):
            counts["reworded"] += 1
        expected.append(
            {
                "title": item["title"],
                "source_quote": metadata.get("source_quote") or "",
                "deliverable": metadata.get("deliverable") or "",
                "owner_name": metadata.get("suggested_owner_name"),
                "deadline_iso": metadata.get("suggested_deadline_iso"),
                "label": "KEPT",
                "grounded": bool(metadata.get("source_timestamp")),
            }
        )

    rejected_quotes = [
        json.loads(
            dict(row).get("proposal_metadata") or "{}"
            if isinstance(dict(row).get("proposal_metadata"), str)
            else "{}"
        ).get("source_quote")
        or ""
        for row in rows
        if dict(row)["status"] == "REJECTED"
    ]

    return {
        "schema_version": "harvested-gold.v1",
        "episode_id": episode_id,
        "run_id": run_id,
        "expected_items": expected,
        # Kept beside the positives because "stopped proposing this" is an
        # improvement the positive list cannot express.
        "rejected_quotes": [quote for quote in rejected_quotes if quote],
        "counts": counts,
        "provenance": (
            "由会议负责人在真实使用中的复核决定收割而来，不是独立标注。"
            "这适合衡量抽取对这支团队有没有用，不适合作为通用结论——"
            "做判断的人当时在会议室里，这既是它的强处也是它的边界。"
        ),
    }


def write_gold(
    database: Any,
    *,
    episode_id: str,
    run_id: str,
    destination: str | Path,
) -> dict[str, Any]:
    """Harvest one meeting and write it where the evaluation can read it."""

    payload = harvest(database, episode_id=episode_id, run_id=run_id)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def review_confusion(payload: dict[str, Any]) -> dict[str, Any]:
    """Precision and recall as the review decisions actually support them.

    The tempting move is to feed the harvested list back through `score_items`
    and read the F1. That would be circular for most of it: the kept items are
    the extractor's own output, so matching them against itself measures
    nothing. Only two of the four decisions carry information the extractor did
    not supply.

    Precision is sound. Every proposal was looked at, and the ones marked
    `ignored` were rejected by the person who would have had to do the work --
    an unbiased count of false positives over a complete population.

    Recall is a *lower bound*, and the returned value says so in its own key.
    It counts what the coordinator noticed was missing, not what a careful
    annotator reading the transcript would find. A task nobody remembered is
    absent from both the extraction and the gold, and no amount of usage
    surfaces it. Reporting this as "recall" full stop would be the most
    flattering number in the file and the least true.
    """

    counts = payload["counts"]
    kept = counts["kept"]
    ignored = counts["ignored"]
    added = counts["added"]

    proposed = kept + ignored
    precision = round(kept / proposed, 4) if proposed else None
    recall_floor = round(kept / (kept + added), 4) if (kept + added) else None
    f1_floor = (
        round(2 * precision * recall_floor / (precision + recall_floor), 4)
        if precision and recall_floor and (precision + recall_floor)
        else None
    )
    return {
        "proposed": proposed,
        "kept": kept,
        "rejected_by_reviewer": ignored,
        "added_by_reviewer": added,
        "precision": precision,
        "recall_lower_bound": recall_floor,
        "f1_upper_bound": f1_floor,
        "why_recall_is_a_bound": (
            "只统计了会议负责人注意到漏掉的条目。"
            "一件所有人都忘了的事，在抽取和金标里同时缺席，"
            "再多的使用也不会让它浮出来。"
        ),
    }


def combine(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool several meetings, because one meeting is an anecdote."""

    total = {"kept": 0, "ignored": 0, "added": 0, "reworded": 0}
    for payload in payloads:
        for key in total:
            total[key] += payload["counts"].get(key, 0)
    pooled = review_confusion({"counts": total})
    pooled["meetings"] = len(payloads)
    return pooled
