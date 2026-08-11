"""Turn the pilot labels into the four numbers that decide the next step.

Validation runs first and refuses to print conclusions when it fails, because a
label file with a mangled candidate_id produces numbers that look fine and are
wrong.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LABELS = {"DRAFT", "HINT", "HIDE"}
REASONS = {"REAL", "WEAK", "COND", "DISC", "BG", "DUP", "CHAT"}

# How many review cards a coordinator is assumed to tolerate per meeting.
CARD_BUDGET = 10


def read_tsv(path: Path) -> list[dict[str, str]]:
    """Read a label file whatever Excel decided to save it as.

    Excel on a Chinese Windows rewrites a UTF-8 TSV as GBK the moment it saves,
    which is not something the annotator can be expected to notice. Decoding
    strictly as UTF-8 turns that into either a crash or, worse, mojibake that
    scores fine and means nothing.
    """

    if not path.exists():
        return []
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"{path} 无法按 UTF-8 或 GB18030 解码")
    return [
        dict(row)
        for row in csv.DictReader(text.splitlines(), delimiter="\t")
    ]


def load_meeting(directory: Path) -> dict[str, Any] | None:
    sidecar = directory / "candidates.json"
    if not sidecar.exists():
        return None
    with sidecar.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["labels"] = read_tsv(directory / "candidates.tsv")
    payload["missed"] = read_tsv(directory / "missed.tsv")
    return payload


def validate(meeting: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    known = {c["candidate_id"] for c in meeting["candidates"]}
    seen: set[str] = set()
    labelled = 0
    for row in meeting["labels"]:
        cid = (row.get("candidate_id") or "").strip()
        if cid not in known:
            problems.append(f"未知 candidate_id: {cid!r}")
            continue
        if cid in seen:
            problems.append(f"candidate_id 重复出现: {cid}")
        seen.add(cid)
        label = (row.get("label") or "").strip().upper()
        reason = (row.get("reason") or "").strip().upper()
        cluster = (row.get("cluster") or "").strip()
        if not label:
            continue
        labelled += 1
        if label not in LABELS:
            problems.append(f"{cid}: label {label!r} 不合法")
        if reason and reason not in REASONS:
            problems.append(f"{cid}: reason {reason!r} 不合法")
        if reason == "DUP" and not cluster:
            problems.append(f"{cid}: reason=DUP 但没有填 cluster")
    if labelled == 0:
        problems.append("一条都还没标")
    elif labelled < len(known):
        problems.append(f"只标了 {labelled}/{len(known)} 条（未标完不出结论）")
    return problems


def score(meetings: list[dict[str, Any]]) -> dict[str, Any]:
    per_meeting = []
    confusion: Counter[tuple[str, str]] = Counter()
    reason_by_route: Counter[tuple[str, str]] = Counter()
    system_reason_vs_human: Counter[tuple[str, str]] = Counter()

    for meeting in meetings:
        system = {c["candidate_id"]: c for c in meeting["candidates"]}
        rows = [r for r in meeting["labels"] if (r.get("label") or "").strip()]

        clusters: dict[str, list[dict[str, str]]] = defaultdict(list)
        wanted_clusters: set[str] = set()
        human_counts: Counter[str] = Counter()

        for row in rows:
            cid = row["candidate_id"].strip()
            label = row["label"].strip().upper()
            reason = (row.get("reason") or "").strip().upper()
            cluster = (row.get("cluster") or "").strip() or f"_{cid}"
            route = system[cid]["system_route"]
            human_counts[label] += 1
            confusion[(route, label)] += 1
            reason_by_route[(route, reason)] += 1
            system_reason_vs_human[(system[cid]["system_reason_code"], label)] += 1
            clusters[cluster].append({"cid": cid, "label": label})
            if label in {"DRAFT", "HINT"}:
                wanted_clusters.add(cluster)

        noise_clusters = {
            key for key, members in clusters.items()
            if all(m["label"] == "HIDE" for m in members)
        }

        # Fourth number: with only CARD_BUDGET cards, how much of what the
        # coordinator wanted still arrives? Drafts first in transcript order is
        # what the product does today; the oracle is the ceiling a perfect
        # ranker could reach, so the gap is what reranking is worth.
        order = sorted(
            rows,
            key=lambda r: (
                0 if system[r["candidate_id"].strip()]["system_route"] == "DRAFT" else 1,
                system[r["candidate_id"].strip()]["anchor"],
            ),
        )
        wanted_draft_clusters = {
            (r.get("cluster") or "").strip() or f"_{r['candidate_id'].strip()}"
            for r in rows
            if r["label"].strip().upper() == "DRAFT"
        }
        shown: set[str] = set()
        for row in order[:CARD_BUDGET]:
            cluster = (row.get("cluster") or "").strip() or f"_{row['candidate_id'].strip()}"
            shown.add(cluster)
        covered = len(shown & wanted_draft_clusters)
        oracle = min(CARD_BUDGET, len(wanted_draft_clusters))

        missed = [m for m in meeting["missed"] if (m.get("what_should_happen") or "").strip()]
        per_meeting.append({
            "meeting_id": meeting["meeting_id"],
            "amc_gold_positive_sentences": meeting["amc_gold_positive_sentences"],
            "candidates": len(rows),
            "human": dict(human_counts),
            "clusters_total": len(clusters),
            "clusters_wanted": len(wanted_clusters),
            "clusters_pure_noise": len(noise_clusters),
            "candidates_per_wanted_cluster": (
                round(len(rows) / len(wanted_clusters), 2) if wanted_clusters else None
            ),
            "draft_clusters_wanted": len(wanted_draft_clusters),
            "card_budget_covered": covered,
            "card_budget_ceiling": oracle,
            "card_budget_recall": (
                round(covered / len(wanted_draft_clusters), 4)
                if wanted_draft_clusters else None
            ),
            "missed_entirely": len(missed),
        })

    total_rows = sum(m["candidates"] for m in per_meeting)
    kept = sum(m["human"].get("DRAFT", 0) + m["human"].get("HINT", 0) for m in per_meeting)
    return {
        "schema_version": "annotation-pilot-score.v1",
        "card_budget": CARD_BUDGET,
        "per_meeting": per_meeting,
        "headline": {
            # 1. how much of what AMC-A rejects is product-legitimate
            "labelled_candidates": total_rows,
            "product_legitimate": kept,
            "product_legitimate_rate": round(kept / total_rows, 4) if total_rows else None,
            "hide_rate": round(
                sum(m["human"].get("HIDE", 0) for m in per_meeting) / total_rows, 4
            ) if total_rows else None,
            # 2/3. distinct things, not distinct sentences
            "clusters_total": sum(m["clusters_total"] for m in per_meeting),
            "clusters_pure_noise": sum(m["clusters_pure_noise"] for m in per_meeting),
            "candidates_per_cluster": round(
                total_rows / sum(m["clusters_total"] for m in per_meeting), 2
            ) if sum(m["clusters_total"] for m in per_meeting) else None,
            # 4. recall under a real display budget
            "card_budget_covered": sum(m["card_budget_covered"] for m in per_meeting),
            "card_budget_wanted": sum(m["draft_clusters_wanted"] for m in per_meeting),
            "card_budget_ceiling": sum(m["card_budget_ceiling"] for m in per_meeting),
            "missed_entirely": sum(m["missed_entirely"] for m in per_meeting),
        },
        "confusion_system_route_vs_human": {
            f"{route}->{label}": count for (route, label), count in sorted(confusion.items())
        },
        "human_reason_by_system_route": {
            f"{route}->{reason}": count
            for (route, reason), count in sorted(reason_by_route.items()) if reason
        },
        "system_reason_code_vs_human_label": {
            f"{code}->{label}": count
            for (code, label), count in sorted(system_reason_vs_human.items()) if code
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", default="var/annotation/pilot5")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    root = Path(args.pilot)
    meetings = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        meeting = load_meeting(directory)
        if meeting:
            meetings.append(meeting)
    if not meetings:
        raise SystemExit(f"{root} 下没有找到标注包")

    blocked = False
    for meeting in meetings:
        problems = validate(meeting)
        if problems:
            blocked = True
            print(f"[{meeting['meeting_id']}] {len(problems)} 处问题")
            for problem in problems[:10]:
                print(f"    - {problem}")
    if blocked:
        raise SystemExit("\n先修掉上面的问题；未标完或有非法值时算出的结论是错的。")

    report = score(meetings)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    head = report["headline"]
    print(f"标注候选           {head['labelled_candidates']}")
    print(f"产品口径合法       {head['product_legitimate']} "
          f"({head['product_legitimate_rate']:.1%})")
    print(f"纯噪音             {head['hide_rate']:.1%}")
    print(f"独立事项(cluster)  {head['clusters_total']}  "
          f"其中纯噪音簇 {head['clusters_pure_noise']}")
    print(f"每个事项占候选数   {head['candidates_per_cluster']}")
    print(f"{report['card_budget']} 卡预算覆盖    "
          f"{head['card_budget_covered']}/{head['card_budget_wanted']} "
          f"(上限 {head['card_budget_ceiling']})")
    print(f"完全漏掉的事项     {head['missed_entirely']}")
    print(f"\n{text}" if not args.out else f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
