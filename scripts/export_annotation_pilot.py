"""Export a product-caliber annotation package for a handful of meetings.

The scoring runs answer "how does the system do against AMC-A", which cannot
answer the question that actually blocks the next engineering decision: of the
candidates AMC-A calls wrong, how many would a coordinator still want to see?
That question needs a human, and a human needs the transcript next to the
candidates.

Two deliberate omissions from the labelling surface:

* the AMC-A gold labels -- an annotator who can see which candidate is already
  a true positive will anchor on it, and the whole point is to measure the gap
  between the two calibers independently;
* the system's own routing decision -- DRAFT/HINT is joined back by
  candidate_id after labelling, so the confusion matrix is between two
  independent judgements rather than one judgement and its own echo.

Both live in the JSON sidecar, which the scorer reads and the annotator does not.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
from pathlib import Path
from typing import Any

# Ordered so the annotator meets the cheap decisions first; CHAT is separated
# from DISCUSSION because a corpus that is mostly transcription noise wants a
# pre-filter, and one that is mostly real discussion wants a reranker. Merging
# them would hide which of those two we are looking at.
REASON_CODES = [
    ("REAL", "真实行动项：会后确实有人要做一件事"),
    ("WEAK", "弱决策：倾向已出现但没拍板"),
    ("COND", "条件句/假设：如果…就…，尚未成立"),
    ("DISC", "讨论/发散：在聊这件事，没有形成决定"),
    ("BG", "背景事实/既定前提/惯例描述"),
    ("DUP", "与本场另一条候选是同一件事（必须填 cluster）"),
    ("CHAT", "寒暄、无关内容或转写噪音"),
]

LABELS = [
    ("DRAFT", "负责人希望直接看到的任务草稿"),
    ("HINT", "信息不足，但值得作为线索提醒"),
    ("HIDE", "完全不值得出现"),
]


def load_report(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_checkpoints(directory: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name in glob.glob(str(directory / "*.json")):
        with open(name, encoding="utf-8") as handle:
            payload = json.load(handle)
        artifact = payload.get("artifact")
        if artifact:
            artifacts[name] = artifact
    return artifacts


def match_meeting_to_artifact(
    predictions: dict[str, list[dict[str, Any]]],
    artifacts: dict[str, dict[str, Any]],
    meeting_id: str,
) -> dict[str, Any] | None:
    """Identity is the candidate_id set, not the sentence count.

    Unit counts collide across meetings because units are sub-sentence splits,
    so matching on them silently picks the wrong transcript.
    """

    wanted = {p["candidate_id"] for p in predictions.get(meeting_id, [])}
    if not wanted:
        return None
    best: dict[str, Any] | None = None
    best_overlap = 0
    for artifact in artifacts.values():
        have = {c["candidate_id"] for c in artifact.get("raw_candidates", [])}
        overlap = len(wanted & have)
        if overlap > best_overlap:
            best_overlap = overlap
            best = artifact
    if best is None or best_overlap < len(wanted):
        return None
    return best


def build_unit_index(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["unit_id"]: unit for unit in artifact.get("units", [])}


def route_of(artifact: dict[str, Any]) -> dict[str, tuple[str, str]]:
    routes: dict[str, tuple[str, str]] = {}
    for item in artifact.get("draft_items", []):
        routes[item["candidate_id"]] = ("DRAFT", item.get("reason_code") or "")
    for hint in artifact.get("review_hints", []):
        routes.setdefault(
            hint["candidate_id"], ("HINT", hint.get("reason_code") or "")
        )
    for raw in artifact.get("raw_candidates", []):
        routes.setdefault(
            raw["candidate_id"], ("UNROUTED", raw.get("reason_code") or "")
        )
    return routes


def context_window(
    units: list[dict[str, Any]], anchor_index: int, radius: int = 1
) -> str:
    start = max(0, anchor_index - radius)
    end = min(len(units), anchor_index + radius + 1)
    parts = []
    for unit in units[start:end]:
        marker = "»" if unit["index"] == anchor_index else " "
        parts.append(f"{marker}{unit.get('speaker','')}: {unit.get('text','')}")
    return " / ".join(parts)


def export_meeting(
    meeting_id: str,
    artifact: dict[str, Any],
    gold_sentences: int,
    out_root: Path,
) -> dict[str, Any]:
    out = out_root / meeting_id
    out.mkdir(parents=True, exist_ok=True)
    units = artifact.get("units", [])
    index = build_unit_index(artifact)
    routes = route_of(artifact)

    transcript_path = out / "transcript.txt"
    with transcript_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# {meeting_id}　共 {len(units)} 个发言单元\n")
        handle.write("# 行首编号是 unit index，候选表的 anchor 列指向它\n\n")
        for unit in units:
            handle.write(
                f"[{unit['index']:>4}] {unit.get('timestamp','')} "
                f"{unit.get('speaker','')}: {unit.get('text','')}\n"
            )

    rows: list[dict[str, Any]] = []
    for raw in artifact.get("raw_candidates", []):
        anchors = [index[uid] for uid in raw.get("anchor_unit_ids", []) if uid in index]
        anchor_index = anchors[0]["index"] if anchors else -1
        timestamp = anchors[0].get("timestamp", "") if anchors else ""
        quote = " ".join(unit.get("text", "") for unit in anchors)
        route, reason_code = routes.get(raw["candidate_id"], ("UNROUTED", ""))
        rows.append(
            {
                "candidate_id": raw["candidate_id"],
                "anchor": anchor_index,
                "timestamp": timestamp,
                "quote": quote,
                "context": context_window(units, anchor_index) if anchors else "",
                "system_route": route,
                "system_reason_code": reason_code,
                "kind_hints": ",".join(raw.get("kind_hints") or []),
                "trigger_sources": ",".join(raw.get("trigger_sources") or []),
                "support_units": len(raw.get("support_unit_ids") or []),
            }
        )
    rows.sort(key=lambda row: (row["anchor"], row["candidate_id"]))

    # The annotator's surface: transcript order, no gold, no system decision.
    tsv_path = out / "candidates.tsv"
    with tsv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            ["candidate_id", "anchor", "timestamp", "quote", "context",
             "label", "cluster", "reason", "note"]
        )
        for row in rows:
            writer.writerow(
                [row["candidate_id"], row["anchor"], row["timestamp"],
                 row["quote"], row["context"], "", "", "", ""]
            )

    missed_path = out / "missed.tsv"
    with missed_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["anchor", "timestamp", "what_should_happen", "why_wanted"])

    sidecar_path = out / "candidates.json"
    with sidecar_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "meeting_id": meeting_id,
                "amc_gold_positive_sentences": gold_sentences,
                "units": len(units),
                "candidates": rows,
            },
            handle,
            ensure_ascii=False,
            indent=1,
        )

    drafts = sum(1 for row in rows if row["system_route"] == "DRAFT")
    return {
        "meeting_id": meeting_id,
        "units": len(units),
        "candidates": len(rows),
        "system_draft": drafts,
        "system_hint": len(rows) - drafts,
        "amc_gold_positive_sentences": gold_sentences,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        default="evaluation_runs/recall-selected-v23-pilot15-final-v2.json",
    )
    parser.add_argument(
        "--checkpoints",
        default="evaluation_runs/.checkpoints/recall-selected-v23-full82/union_selected_v2",
    )
    parser.add_argument("--extractor", default="union_selected_v2")
    parser.add_argument("--out", default="var/annotation/pilot5")
    parser.add_argument(
        "--meetings",
        nargs="+",
        default=["M2869", "M3246", "M2857", "M3179", "M3284"],
    )
    args = parser.parse_args()

    report = load_report(Path(args.report))
    result = report["results"][args.extractor]
    predictions = result["predictions"]
    per_meeting = {m["meeting_id"]: m for m in result["per_meeting"]}
    artifacts = load_checkpoints(Path(args.checkpoints))

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    summaries = []
    for meeting_id in args.meetings:
        artifact = match_meeting_to_artifact(predictions, artifacts, meeting_id)
        if artifact is None:
            raise SystemExit(f"no checkpoint artifact covers {meeting_id}")
        gold = per_meeting[meeting_id]["gold_positive_sentences"]
        summaries.append(export_meeting(meeting_id, artifact, gold, out_root))

    manifest = {
        "schema_version": "annotation-pilot.v1",
        "source_report": args.report,
        "extractor": args.extractor,
        # Which build produced these candidates. Re-exporting after a code
        # change must not silently reuse labels attached to older candidate ids.
        "frozen_state": report.get("frozen_state"),
        "corpus_meetings": report["corpus"]["meetings"],
        "corpus_gold_positive_sentences": report["corpus"]["gold_positive_sentences"],
        "selected": summaries,
        "totals": {
            "candidates": sum(s["candidates"] for s in summaries),
            "system_draft": sum(s["system_draft"] for s in summaries),
            "system_hint": sum(s["system_hint"] for s in summaries),
            "amc_gold_positive_sentences": sum(
                s["amc_gold_positive_sentences"] for s in summaries
            ),
        },
        "labels": dict(LABELS),
        "reason_codes": dict(REASON_CODES),
    }
    with (out_root / "MANIFEST.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)

    for summary in summaries:
        print(
            f"{summary['meeting_id']:<8} units={summary['units']:>5} "
            f"candidates={summary['candidates']:>4} "
            f"(system draft={summary['system_draft']}, hint={summary['system_hint']}) "
            f"amc_gold={summary['amc_gold_positive_sentences']}"
        )
    print(f"\ntotal candidates to label: {manifest['totals']['candidates']}")
    print(f"written to {out_root}")


if __name__ == "__main__":
    main()
