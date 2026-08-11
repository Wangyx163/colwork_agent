"""Compare recall-window policies on one frozen AMC-A meeting manifest.

The experiment scores raw ``model union rule`` candidates, deliberately
skipping candidate structuring so a window decision is not confounded by the
later draft/hint or schema stages. Provider payloads are checkpointed per
window and evaluation results per meeting, making interrupted runs resumable.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from collab_agent.extraction_baselines import (
    project_chain_extractor,
    rule_recall_extractor,
)
from collab_agent.extraction_evaluation import compare_extractors, load_alimeeting4mug
from collab_agent.recall import WindowPolicy


COMMON_EIGHT = (
    "M3115",
    "M3253",
    "M3214",
    "M2869",
    "M3246",
    "M2471",
    "M2990",
    "M2857",
)

PROFILES = {
    "current": WindowPolicy(
        total_characters=4000,
        left_characters=1000,
        emit_characters=2000,
        right_characters=1000,
    ),
    "context800": WindowPolicy(
        total_characters=3600,
        left_characters=800,
        emit_characters=2000,
        right_characters=800,
    ),
    "context600": WindowPolicy(
        total_characters=3200,
        left_characters=600,
        emit_characters=2000,
        right_characters=600,
    ),
    "context400": WindowPolicy(
        total_characters=2800,
        left_characters=400,
        emit_characters=2000,
        right_characters=400,
    ),
    "emit1200": WindowPolicy(
        total_characters=2000,
        left_characters=400,
        emit_characters=1200,
        right_characters=400,
    ),
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dataset", default="datasets/Alimeeting4MUG")
    result.add_argument("--split", default="except_TS_test1")
    result.add_argument(
        "--profile",
        action="append",
        choices=tuple(PROFILES),
        help="repeat to run selected profiles; defaults to all",
    )
    result.add_argument(
        "--window",
        action="append",
        metavar="NAME:LEFT:EMIT:RIGHT",
        help="add an explicit character-budget profile without editing this script",
    )
    result.add_argument(
        "--meeting-id",
        action="append",
        help="repeat to freeze an explicit manifest; defaults to the common eight",
    )
    result.add_argument("--all-meetings", action="store_true")
    result.add_argument(
        "--report", default="evaluation_runs/recall-window-sweep-common8.json"
    )
    result.add_argument("--checkpoint-dir", default="")
    result.add_argument(
        "--usage-reference-report",
        default="",
        help="copy audited provider usage when replaying the same window responses",
    )
    result.add_argument("--workers", type=int, default=4)
    result.add_argument("--no-resume", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    all_meetings = load_alimeeting4mug(args.dataset, split=args.split)
    by_id = {meeting.meeting_id: meeting for meeting in all_meetings}
    requested_ids = (
        tuple(by_id)
        if args.all_meetings
        else tuple(args.meeting_id or COMMON_EIGHT)
    )
    missing = [meeting_id for meeting_id in requested_ids if meeting_id not in by_id]
    if missing:
        raise SystemExit(f"unknown meeting ids: {missing}")
    meetings = [by_id[meeting_id] for meeting_id in requested_ids]

    report_path = Path(args.report)
    checkpoint_root = (
        Path(args.checkpoint_dir)
        if args.checkpoint_dir
        else report_path.parent / ".checkpoints" / report_path.stem
    )
    selected_profiles = {
        name: PROFILES[name]
        for name in (args.profile or (() if args.window else PROFILES))
    }
    for specification in args.window or []:
        try:
            name, raw_left, raw_emit, raw_right = specification.split(":", 3)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("profile name must be alphanumeric, underscore or dash")
            left, emit, right = map(int, (raw_left, raw_emit, raw_right))
            if name in selected_profiles:
                raise ValueError(f"duplicate profile name {name}")
            selected_profiles[name] = WindowPolicy(
                total_characters=left + emit + right,
                left_characters=left,
                emit_characters=emit,
                right_characters=right,
            )
        except ValueError as error:
            raise SystemExit(f"invalid --window {specification!r}: {error}") from error
    extractors = {"rule_raw_candidates": rule_recall_extractor(output="raw_candidates")}
    for name, policy in selected_profiles.items():
        extractors[f"union_{name}"] = project_chain_extractor(
            checkpoint_dir=str(checkpoint_root / "_windows" / name),
            window_policy=policy,
            output="raw_candidates",
            structure_candidates=False,
            discovery_workers=args.workers,
        )

    report = compare_extractors(
        meetings,
        extractors,
        checkpoint_dir=checkpoint_root,
        resume=not args.no_resume,
    )
    report["experiment"] = {
        "kind": "RECALL_WINDOW_SWEEP",
        "split": args.split,
        "meeting_ids": list(requested_ids),
        "gold_positive_sentences": sum(
            len(meeting.positive_sentence_indices) for meeting in meetings
        ),
        "profiles": {
            name: {
                "total_characters": policy.total_characters,
                "left_characters": policy.left_characters,
                "emit_characters": policy.emit_characters,
                "right_characters": policy.right_characters,
                "max_unit_characters": policy.max_unit_characters,
            }
            for name, policy in selected_profiles.items()
        },
        "scoring_surface": "RAW_MODEL_UNION_RULE_CANDIDATES",
        "structuring_model_calls": False,
    }
    if args.usage_reference_report:
        usage_reference_path = Path(args.usage_reference_report)
        usage_reference = json.loads(
            usage_reference_path.read_text(encoding="utf-8")
        )
        reference_experiment = usage_reference.get("experiment") or {}
        if (
            reference_experiment.get("meeting_ids")
            != report["experiment"]["meeting_ids"]
            or reference_experiment.get("profiles")
            != report["experiment"]["profiles"]
            or (usage_reference.get("frozen_state") or {}).get(
                "recall_prompt_version"
            )
            != (report.get("frozen_state") or {}).get("recall_prompt_version")
        ):
            raise SystemExit(
                "usage reference does not match meeting manifest, windows, and prompt"
            )
        copied: dict[str, str] = {}
        for name, result in report["results"].items():
            reference_result = (usage_reference.get("results") or {}).get(name)
            if not isinstance(reference_result, dict):
                continue
            current_usage = result.get("usage_totals") or {}
            reference_usage = reference_result.get("usage_totals") or {}
            if (
                not current_usage.get("calls_with_usage")
                and reference_usage.get("calls_with_usage")
            ):
                result["usage_totals"] = reference_usage
                result["usage_replayed_from"] = str(
                    usage_reference_path.resolve()
                )
                copied[name] = str(usage_reference_path.resolve())
        report["usage_references"] = copied
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = {}
    for name, result in report["results"].items():
        per_meeting = result["per_meeting"]
        summary[name] = {
            "sentence_level": result["sentence_level_positive_f1"],
            "recall_gold_coverage": result["recall_gold_coverage"],
            "zero_hit_positive_meetings": sum(
                row["gold_positive_sentences"] > 0
                and row["sentence_level"]["true_positive"] == 0
                for row in per_meeting
            ),
            "raw_candidates": result["stage_totals"]["raw_candidates"],
            "rule_candidates": result["stage_totals"]["rule_candidates"],
            "model_candidates": result["stage_totals"]["model_candidates"],
            "anchor_unit_references": result["stage_totals"][
                "anchor_unit_references"
            ],
            "support_unit_references": result["stage_totals"][
                "support_unit_references"
            ],
            "evidence_unit_references": result["stage_totals"][
                "evidence_unit_references"
            ],
            "evidence_bridge_unit_references": result["stage_totals"][
                "evidence_bridge_unit_references"
            ],
            "model_support_trimmed_candidates": result["stage_totals"][
                "model_support_trimmed_candidates"
            ],
            "model_unit_id_canonicalized_candidates": result["stage_totals"][
                "model_unit_id_canonicalized_candidates"
            ],
            "visible_context_anchor_recovered_candidates": result[
                "stage_totals"
            ]["visible_context_anchor_recovered_candidates"],
            "visible_context_duplicate_merged_candidates": result[
                "stage_totals"
            ]["visible_context_duplicate_merged_candidates"],
            "model_windows": result["stage_totals"]["windows"],
            "model_windows_succeeded": result["stage_totals"][
                "model_windows_succeeded"
            ],
            "usage": result["usage_totals"],
            "pipeline_statuses": {
                status: sum(
                    row.get("stages", {}).get("pipeline_status") == status
                    for row in per_meeting
                )
                for status in ("SUCCEEDED", "DEGRADED", "FAILED")
            },
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
