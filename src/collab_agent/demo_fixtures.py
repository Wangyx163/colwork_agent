"""Turn a validated annotation into an extraction file, for stable demos.

A demo that calls the model re-runs a non-deterministic step in front of an
audience: the same meeting can yield a different set of candidates, and a rate
limit or a timeout takes the demo with it. Deriving the extraction from the
gold annotation removes both -- the meeting always loads with exactly the
items a person already checked.

This is a demo aid, not an evaluation shortcut. The output records
`provider: "gold-annotation"` so nothing downstream can mistake it for model
output, and scoring an extractor against the same gold file it was derived
from would obviously report a perfect score, which is why the two paths are
kept visibly distinct.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .annotation_check import check_annotation_file
from .models import read_text_file, stable_hash


GOLD_DERIVED_PROVIDER = "gold-annotation"


def gold_case_to_extraction(case: dict[str, Any]) -> dict[str, Any]:
    """Render one annotated case in the extraction schema."""

    transcript = str(case.get("transcript") or "")
    items: list[dict[str, Any]] = []
    for expected in case.get("expected") or []:
        items.append(
            {
                "title": expected["title"],
                "item_type": expected.get("item_type", "TASK"),
                "deliverable": expected["deliverable"],
                "owner_name": expected.get("owner_name"),
                "deadline_text": expected.get("deadline_text"),
                "deadline_iso": expected.get("deadline_iso"),
                "source_timestamp": expected["source_timestamp"],
                "source_quote": expected["source_quote"],
                # A human checked these, so confidence is 1.0 -- but every item
                # still needs confirmation, because the coordinator reviewing a
                # task is a workflow step, not a statement about accuracy.
                "confidence": 1.0,
                "needs_confirmation": True,
                "uncertainties": [],
                "collaborator_names": list(expected.get("collaborator_names") or []),
            }
        )
    return {
        "schema_version": "1.0",
        "provider": GOLD_DERIVED_PROVIDER,
        "model": "n/a",
        "prompt_version": "n/a",
        "input_sha256": stable_hash(transcript),
        "input_characters": len(transcript),
        "action_items": items,
        "source": {
            "case_id": case.get("case_id"),
            "meeting_date": case.get("meeting_date"),
            "participants": list(case.get("participants") or []),
            "derived_from_annotation": True,
        },
    }


def gold_to_extraction(
    gold_path: str | Path,
    output_path: str | Path,
    *,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Convert a gold file, refusing to proceed if it does not validate.

    The check is not optional: an annotation with an unlocatable quote would
    load into a meeting whose items cite text that is not in the transcript,
    which is the one thing this project's evidence rules exist to prevent.
    """

    report = check_annotation_file(gold_path)
    if not report["valid"]:
        errors = [p for p in report["problems"] if p["level"] == "ERROR"]
        raise ValueError(
            f"{gold_path} 未通过标注校验（{len(errors)} 个错误），先修复再转换："
            + "; ".join(str(problem["message"]) for problem in errors[:3])
        )

    payload = json.loads(read_text_file(gold_path))
    cases = payload["cases"]
    if case_id:
        matching = [case for case in cases if case.get("case_id") == case_id]
        if not matching:
            raise ValueError(f"{gold_path} 里没有 case_id={case_id}")
        case = matching[0]
    elif len(cases) == 1:
        case = cases[0]
    else:
        raise ValueError(
            f"{gold_path} 含 {len(cases)} 个 case，请用 --case-id 指定一个"
        )

    extraction = gold_case_to_extraction(case)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(extraction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "output": str(destination.resolve()),
        "case_id": case.get("case_id"),
        "action_items": len(extraction["action_items"]),
        "participants": extraction["source"]["participants"],
        "provider": GOLD_DERIVED_PROVIDER,
    }
