"""Render one `link propose --show-scores` run as a readable table.

The JSON that command emits is complete but unreadable at a glance, and the
point of this demo is a comparison somebody can see: lexical similarity does
not separate related work from unrelated work, and semantic similarity does.
A wall of nested objects hides exactly that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collab_agent.embeddings import SEMANTIC_LINK_THRESHOLD  # noqa: E402
from collab_agent.models import read_text_file  # noqa: E402


def bar(value: float, width: int = 18) -> str:
    filled = max(0, min(width, round(value * width)))
    return "█" * filled + "·" * (width - filled)


def main(path: str) -> int:
    report = json.loads(read_text_file(path))
    scoreboard = report.get("scoreboard") or []
    if not scoreboard:
        print("没有候选可比较：候选池为空。检查两场会议是否有同一个人参加。")
        return 1

    cache = report.get("embedding_cache") or {}
    print(
        f"扫描 {report['action_items_scanned']} 条任务  "
        f"embedding 命中 {cache.get('hits', 0)} / 新算 {cache.get('misses', 0)}  "
        f"阈值 {SEMANTIC_LINK_THRESHOLD}"
    )
    print()
    print("每条任务与其最相似的历史任务：")
    print()
    print(f"{'字面':>6} {'语义':>6}  {'':<20}")

    crossed = 0
    for entry in scoreboard:
        top = entry["candidates"][0]
        semantic = top["semantic"]
        over = semantic is not None and semantic >= SEMANTIC_LINK_THRESHOLD
        crossed += 1 if over else 0
        mark = "★" if over else " "
        print(
            f"{top['lexical']:6.3f} {semantic:6.3f}  {mark} {bar(semantic)}"
        )
        print(f"{'':>13}   新: {entry['task']}")
        print(f"{'':>13}   旧: {top['prior_task']}")

    lexical_values = [entry["candidates"][0]["lexical"] for entry in scoreboard]
    print()
    print(
        f"字面相似度全部落在 {min(lexical_values):.3f}–{max(lexical_values):.3f}，"
        "真延续与无关任务混在一起，无法区分。"
    )
    print(f"语义相似度把 {crossed} 条推过阈值，其余留在线下。")

    proposals = report.get("with_proposals") or []
    if proposals:
        print()
        print("模型给出的关联：")
        for item in proposals:
            for proposal in item["proposals"]:
                print(f"  · {item['title']}")
                print(f"    {proposal['relation']}  [{proposal['source']}]")
                print(f"    依据：{proposal['reason']}")
    else:
        print()
        print("模型未给出任何关联（若未加 --with-model，这是预期的）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
