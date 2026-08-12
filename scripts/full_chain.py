"""Drive one meeting end to end through the HTTP API, every link in the chain.

Through HTTP rather than the service, because the point is that a person can
reach each step from a page. A link that only works when called from Python is
exactly the failure this looks for -- and separately from whether the page can
reach a route at all, whether the request it sends is one the route accepts. A
body with the wrong key name arrives, is read as an empty string, and fails on
a rule about something else entirely; that has happened repeatedly.

The last step is the one that keeps the rest honest: it compares the routes
this run actually exercised against every write route the server declares, and
fails when one was never touched. Adding a route without adding a step here is
therefore a failure, not an omission somebody has to notice.

Usage -- against a throwaway database, never real data:

    python -m collab_agent serve-meeting --db var/full-chain.sqlite3
        --port 8799 --extraction fixtures/full_chain_extraction.json
        --transcript fixtures/full_chain_transcript.txt
        --organization 试跑 --coordinator 甲
        --participant 甲 --participant 乙 --participant 丙
    python scripts/full_chain.py 8799

That fixture rather than any other: the run needs at least six dispatchable
tasks to finish, and it is the only one carrying a review hint -- without which
the materialize route cannot be reached, and the coverage check at the end
fails on a fixture shortfall rather than on a real gap.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import time
import urllib.request
from pathlib import Path

BASE = f"http://127.0.0.1:{sys.argv[1]}"
FAILURES: list[str] = []
#: Which write routes this run actually exercised. Reachable-from-a-page and
#: works-when-a-page-calls-it are different claims, and the second is the one
#: that keeps breaking: a request body with the wrong key name reaches the
#: route, is read as an empty string, and fails on a rule about something else.
TOUCHED: set[str] = set()


def template(path: str) -> str:
    """/api/assistance/help_abc/resolve -> /api/assistance/{id}/resolve"""

    parts = [
        "{id}" if re.fullmatch(r"[a-z]+_[0-9a-f]{6,}|ai_[0-9a-f]+", part) else part
        for part in path.split("?")[0].split("/")
    ]
    return "/".join(parts)


def call(method, path, payload=None, token=None):
    if method == "POST":
        TOUCHED.add(template(path))
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return error.code, {"raw": body[:200]}


def step(name, ok, detail=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


actors = {
    a["display_name"]: a["actor_id"]
    for a in call("GET", "/api/session/actors")[1]["actors"]
}
tokens = {
    name: call("POST", "/api/session", {"actor_id": actor})[1]["token"]
    for name, actor in actors.items()
}
lead_name = next(
    a["display_name"]
    for a in call("GET", "/api/session/actors")[1]["actors"]
    if "COORDINATOR" in a["roles"]
)
lead = tokens[lead_name]
helpers = [n for n in actors if n != lead_name]
worker, mate = helpers[0], helpers[1]


def wait_for_processing(action_item_id: str, tries: int = 30) -> None:
    """Acceptance is gated on processing having finished, so wait for it."""

    for _ in range(tries):
        snapshot = call("GET", "/api/state?surface=manage", token=lead)[1]
        row = next(
            (t for t in snapshot["tasks"] if t["action_item_id"] == action_item_id),
            None,
        )
        version = (row or {}).get("latest_version") or {}
        if version.get("processing_status") in {"READY", "FAILED", "NOT_REQUIRED"}:
            return
        time.sleep(1)


state = call("GET", "/api/state?surface=manage", token=lead)[1]
todo = [t for t in state["tasks"] if t["status"] == "PENDING_CONFIRMATION"]
print(f"起点：{len(todo)} 个待派发任务，参会者 {list(actors)}")
if len(todo) < 6:
    # One pass consumes the meeting: tasks get dispatched, accepted, submitted
    # and accepted. Running twice against the same database is not a failure of
    # the system, it is a used-up fixture, and saying so beats an IndexError
    # six lines later.
    sys.exit(
        f"这个库里只剩 {len(todo)} 个待派发任务，跑不完整条链路。"
        " 全链路脚本要在全新的库上跑：删掉 --db 指向的文件，重启服务再跑。"
    )

print("\n=== 1. 抽取候选的处置 ===")
code, _ = call(
    "POST",
    f"/api/action-items/{todo[-1]['action_item_id']}/ignore",
    {"reason": "会上只是提了一句，不是任务", "message_id": "fc-ignore"},
    lead,
)
step("忽略一个误抽的候选", code == 200, str(code))

if len(todo) >= 3:
    code, _ = call(
        "POST",
        f"/api/action-items/{todo[-2]['action_item_id']}/merge",
        {
            "target_action_item_id": todo[0]["action_item_id"],
            "message_id": "fc-merge",
        },
        lead,
    )
    step("把重复的候选合并", code == 200, str(code))

state = call("GET", "/api/state?surface=manage", token=lead)[1]
hints = [h for h in (state.get("review_hints") or []) if h["status"] == "OPEN"]
if hints:
    code, body = call(
        "POST",
        f"/api/review-hints/{hints[0]['hint_id']}/materialize",
        {
            "title": "跟一下海报",
            "deliverable": "海报进度说明",
            "acceptance_criteria": "有结论",
            "work_requirements": "海报进度说明",
            "priority": "P2",
            "team_required_by_sim_time": "2026-08-25T17:00:00+10:00",
            "message_id": "fc-materialize",
        },
        lead,
    )
    step("把召回提示变成任务", code == 200, f"{code} {str(body.get('message'))[:60]}")
else:
    step("抽取里带着召回提示", False, "这份抽取没有 review_hints，materialize 走不到")

code, body = call(
    "POST",
    "/api/action-items",
    {
        "title": "补一条抽取没抽到的",
        "deliverable": "一页说明",
        "source_note": "散会前口头提的，逐字稿里没录到",
        "acceptance_criteria": "写清楚就行",
        "priority": "P2",
        "message_id": "fc-manual-add",
    },
    lead,
)
step("会议负责人手动补录一条任务", code == 200, f"{code} {str(body.get('message'))[:60]}")
step(
    "补录的任务标着人工来源，不冒充抽取",
    (body or {}).get("origin") == "COORDINATOR_ADDED",
    str(body)[:70],
)

print("\n=== 2. 复核与派发 ===")
task = todo[0]
tid = task["action_item_id"]
meta = task.get("proposal_metadata") or {}
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/revise",
    {
        "title": task["title"],
        "deliverable": meta.get("deliverable") or task["deliverable_key"],
        "acceptance_criteria": "要有结论和依据",
        "priority": "P1",
        "team_required_by_sim_time": "2026-08-20T17:00:00+10:00",
        "message_id": "fc-revise",
    },
    lead,
)
step("复核并定团队时间", code == 200, str(code))
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/dispatch",
    {
        "owner_actor_id": actors[worker],
        "collaborator_actor_ids": [actors[mate]],
        "assignment_message": "麻烦这周内",
        "message_id": "fc-dispatch",
    },
    lead,
)
step(f"派给 {worker}，{mate} 协作", code == 200, str(code))

print("\n=== 3. 派发响应 ===")
for name in (worker, mate):
    code, _ = call(
        "POST",
        f"/api/action-items/{tid}/assignment-response",
        {"decision": "ACCEPT", "response_message": "", "message_id": f"fc-acc-{name}"},
        tokens[name],
    )
    step(f"{name} 接受", code == 200, str(code))

print("\n=== 4. 执行过程 ===")
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/signal",
    {"signal_type": "ON_TRACK", "note": "在做了", "message_id": "fc-signal"},
    tokens[worker],
)
step("报进展", code == 200, str(code))
# A second one, because the quick-status rule needs two before it will
# propose anything -- one signal is an event, two is a habit.
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/signal",
    {"signal_type": "AT_RISK", "note": "可能要晚", "message_id": "fc-signal-2"},
    tokens[worker],
)
step("再报一条进展", code == 200, str(code))
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/personal-commitment",
    {
        "proposed_deadline_sim_time": "2026-08-22T17:00:00+10:00",
        "reason": "手上还有别的",
        "message_id": "fc-commit",
    },
    tokens[worker],
)
step("改自己的承诺时间（制造工期冲突）", code == 200, str(code))
code, _ = call(
    "POST",
    f"/api/action-items/{tid}/amend",
    {
        "title": task["title"] + "（补充范围）",
        "deliverable": "补充了范围说明的交付物",
        "message_id": "fc-amend",
    },
    tokens[worker],
)
step("任务负责人改说明", code == 200, str(code))

code, body = call(
    "POST",
    f"/api/action-items/{tid}/assistance",
    {
        "target_actor_id": actors[mate],
        "category": "EXPERTISE",
        "summary": "这块要你看一下",
        "message_id": "fc-help",
    },
    tokens[worker],
)
step("发起求助", code == 200, str(code))
assistance_id = (body or {}).get("assistance_request_id") or (body or {}).get(
    "assistance_id"
)
if assistance_id:
    code, _ = call(
        "POST",
        f"/api/assistance/{assistance_id}/acknowledge",
        {"message_id": "fc-ack"},
        tokens[mate],
    )
    step(f"{mate} 接手求助", code == 200, str(code))
    code, _ = call(
        "POST",
        f"/api/assistance/{assistance_id}/resolve",
        {"resolution_summary": "一起看过了", "message_id": "fc-resolve"},
        tokens[mate],
    )
    step("标记求助解决", code == 200, str(code))
else:
    step("求助单号回传", False, str(body)[:80])

print("\n=== 5. 提交（带附件）与验收 ===")
attachment = base64.b64encode("结论：可行。依据：三条实测数据。".encode()).decode()
code, body = call(
    "POST",
    f"/api/action-items/{tid}/submit",
    {
        "delivery": {
            "summary": "已完成，结论见附件",
            "content": "正文：三条实测数据支撑结论。",
            "files": [
                {
                    "name": "结论.txt",
                    "type": "text/plain",
                    "size": 60,
                    "data": f"data:text/plain;base64,{attachment}",
                }
            ],
        },
        "message_id": "fc-submit",
    },
    tokens[worker],
)
step("提交成果（含附件）", code == 200, f"{code} 校验={body.get('validation_status')}")
version_id = body.get("version_id")

print("\n=== 6. 自动整理（提交后、验收前）===")
state = call("GET", "/api/state?surface=manage", token=lead)[1]
current = next(t for t in state["tasks"] if t["action_item_id"] == tid)
version = current.get("latest_version") or {}
step(
    "附件被读出正文",
    any(
        item.get("extraction_status") == "EXTRACTED"
        for item in (version.get("attachment_extractions") or [])
    ),
    str([i.get("name") for i in (version.get("attachment_extractions") or [])]),
)


for _ in range(30):
    state = call("GET", "/api/state?surface=manage", token=lead)[1]
    current = next(t for t in state["tasks"] if t["action_item_id"] == tid)
    version = current.get("latest_version") or {}
    if version.get("processing_status") in {"READY", "FAILED"}:
        break
    time.sleep(1)
step(
    "整理跑完（服务进程内的 worker）",
    version.get("processing_status") == "READY",
    str(version.get("processing_status")),
)
step(
    "整理产出了带引用的结果",
    bool(version.get("processing_result")),
    str(list((version.get("processing_result") or {}))[:4]),
)

if version_id:
    code, _ = call(
        "POST",
        f"/api/artifact-versions/{version_id}/review",
        {"approve": True, "comment": "可以", "message_id": "fc-review"},
        lead,
    )
    step("验收通过", code == 200, str(code))

print("\n=== 7. 协作说明书 ===")
lex = call("GET", "/api/state?surface=tasks", token=tokens[worker])[1]
selfd = [
    t
    for t in lex["memory_lexicon"]["topics"]
    if t["origin"] != "SYSTEM_OBSERVED"
]
for topic in selfd:
    code, _ = call(
        "POST",
        "/api/memories/declare",
        {
            "topic": topic["topic"],
            "code": topic["values"][0]["code"],
            "message_id": f"fc-declare-{topic['topic']}",
        },
        tokens[worker],
    )
    if code != 200:
        step(f"答问卷：{topic['title']}", False, str(code))
        break
else:
    step(f"答完 {len(selfd)} 题问卷", True)

after = call("GET", "/api/state?surface=tasks", token=tokens[worker])[1]
mine = [m for m in (after.get("memories") or []) if m["actor_id"] == actors[worker]]
confirmed = [m for m in mine if m["status"] == "CONFIRMED"]
step("问卷答案已确认", len(confirmed) >= len(selfd), f"{len(confirmed)} 条")
if confirmed:
    code, _ = call(
        "POST",
        f"/api/memories/{confirmed[0]['memory_id']}/withdraw",
        {"message_id": "fc-withdraw"},
        tokens[worker],
    )
    step("撤下一条", code == 200, str(code))

drafts = [m for m in mine if m["status"] == "PRIVATE_DRAFT"]
if drafts:
    code, _ = call(
        "POST",
        f"/api/memories/{drafts[0]['memory_id']}/confirm",
        {"message_id": "fc-confirm"},
        tokens[worker],
    )
    step("确认一条系统观察到的", code == 200, str(code))
else:
    step("系统从这次任务观察到候选", False, "没有产出草稿")

print("\n=== 8. 终稿 ===")
code, body = call("POST", "/api/final/generate", {}, lead)
step(
    "任务没验收完时拒绝生成",
    code == 409,
    f"{code} {str(body.get('message'))[:40]}",
)

print("\n=== 9. 协作者贡献 ===")
state = call("GET", "/api/state?surface=manage", token=lead)[1]
spare = [t for t in state["tasks"] if t["status"] == "PENDING_CONFIRMATION"]
contrib_task = spare[0]
cid = contrib_task["action_item_id"]
call(
    "POST",
    f"/api/action-items/{cid}/revise",
    {
        "title": contrib_task["title"],
        "deliverable": "需要两个人一起做的东西",
        "acceptance_criteria": "有结论",
        "priority": "P1",
        "team_required_by_sim_time": "2026-08-25T17:00:00+10:00",
        "message_id": "fc-c-revise",
    },
    lead,
)
call(
    "POST",
    f"/api/action-items/{cid}/dispatch",
    {
        "owner_actor_id": actors[worker],
        "collaborator_actor_ids": [actors[mate]],
        "assignment_message": "",
        "message_id": "fc-c-dispatch",
    },
    lead,
)
for name in (worker, mate):
    call(
        "POST",
        f"/api/action-items/{cid}/assignment-response",
        {"decision": "ACCEPT", "response_message": "", "message_id": f"fc-c-acc-{name}"},
        tokens[name],
    )
code, body = call(
    "POST",
    f"/api/action-items/{cid}/submit",
    {
        "delivery": {"summary": "协作者的材料", "content": "我这边查到的三条"},
        "message_id": "fc-c-contribute",
    },
    tokens[mate],
)
step(f"{mate} 作为协作者交材料", code == 200, str(code))
contribution_version = body.get("version_id")
if contribution_version:
    code, body = call(
        "POST",
        f"/api/artifact-versions/{contribution_version}/contribution",
        {
            "action": "REQUEST_REVISION",
            "comment": "再补一条来源",
            "message_id": "fc-c-revision",
        },
        tokens[worker],
    )
    step("负责人请对方再改", code == 200, f"{code} {str(body.get('message'))[:60]}")
    code, _ = call(
        "POST",
        f"/api/artifact-versions/{contribution_version}/retry-processing",
        {"message_id": "fc-c-retry"},
        lead,
    )
    step("重新排队整理", code in (200, 400), str(code))

print("\n=== 10. 撤销求助 ===")
code, body = call(
    "POST",
    f"/api/action-items/{cid}/assistance",
    {
        "target_actor_id": actors[mate],
        "category": "DECISION",
        "summary": "这个要你拍板",
        "message_id": "fc-help2",
    },
    tokens[worker],
)
second_help = (body or {}).get("assistance_request_id")
if second_help:
    code, _ = call(
        "POST",
        f"/api/assistance/{second_help}/cancel",
        {"message_id": "fc-cancel"},
        tokens[worker],
    )
    step("发起人撤销求助", code == 200, str(code))
else:
    step("求助单号回传（第二条）", False, str(body)[:70])

print("\n=== 11. 复合结构：收集 → 投票 → 定稿 ===")
state = call("GET", "/api/state?surface=manage", token=lead)[1]
free = [t for t in state["tasks"] if t["status"] == "PENDING_CONFIRMATION"]
if len(free) >= 2:
    collect, decide = free[0], free[1]
    code, _ = call(
        "POST",
        "/api/collaboration-structures/question-vote",
        {
            "collection_action_item_ids": [collect["action_item_id"]],
            "decision_action_item_id": decide["action_item_id"],
            "final_owner_actor_id": actors[worker],
            "voter_actor_ids": [actors[worker], actors[mate]],
            # Fewer kept than offered: the domain refuses a ballot that keeps
            # everything, because scoring a list you must take whole decides
            # nothing.
            "selection_count": 2,
            "source_span": decide["source_span"] or "transcript[0]",
            "message_id": "fc-structure",
        },
        lead,
    )
    step("声明一组复合结构", code == 200, str(code))

    rid = collect["action_item_id"]
    call(
        "POST",
        f"/api/action-items/{rid}/revise",
        {
            "title": collect["title"],
            "deliverable": "问题清单",
            "acceptance_criteria": "至少三条",
            "priority": "P1",
            "team_required_by_sim_time": "2026-08-25T17:00:00+10:00",
            "message_id": "fc-collect-revise",
        },
        lead,
    )
    call(
        "POST",
        f"/api/action-items/{rid}/dispatch",
        {
            "owner_actor_id": actors[mate],
            "collaborator_actor_ids": [],
            "assignment_message": "",
            "message_id": "fc-collect-dispatch",
        },
        lead,
    )
    call(
        "POST",
        f"/api/action-items/{rid}/assignment-response",
        {"decision": "ACCEPT", "response_message": "", "message_id": "fc-collect-acc"},
        tokens[mate],
    )
    _, submitted = call(
        "POST",
        f"/api/action-items/{rid}/submit",
        {
            "delivery": {
                "summary": "三条候选问题",
                "content": "1 你最近在忙什么\n2 最难的一步是什么\n3 需要什么支持",
            },
            "message_id": "fc-collect-submit",
        },
        tokens[mate],
    )
    wait_for_processing(rid)
    call(
        "POST",
        f"/api/artifact-versions/{submitted['version_id']}/review",
        {"approve": True, "comment": "可以", "message_id": "fc-collect-review"},
        lead,
    )

    did = decide["action_item_id"]
    call(
        "POST",
        f"/api/action-items/{did}/revise",
        {
            "title": decide["title"],
            "deliverable": "定稿",
            "acceptance_criteria": "取前三条",
            "priority": "P1",
            "team_required_by_sim_time": "2026-08-26T17:00:00+10:00",
            "message_id": "fc-decide-revise",
        },
        lead,
    )
    call(
        "POST",
        f"/api/action-items/{did}/dispatch",
        {
            "owner_actor_id": actors[worker],
            "collaborator_actor_ids": [],
            "assignment_message": "",
            "message_id": "fc-decide-dispatch",
        },
        lead,
    )
    call(
        "POST",
        f"/api/action-items/{did}/assignment-response",
        {"decision": "ACCEPT", "response_message": "", "message_id": "fc-decide-acc"},
        tokens[worker],
    )
    code, body = call(
        "POST",
        f"/api/action-items/{did}/ballot-draft",
        {"message_id": "fc-ballot-draft"},
        tokens[worker],
    )
    step("整理候选", code == 200, f"{code} {str(body.get('message'))[:60]}")

    snapshot = call("GET", "/api/state?surface=tasks", token=tokens[worker])[1]
    row = next(t for t in snapshot["tasks"] if t["action_item_id"] == did)
    ballot = next(
        (
            item
            for item in (row.get("collaboration_progress") or {}).get(
                "contributions", []
            )
            if item["contribution_type"] == "BALLOT"
        ),
        None,
    )
    options = ((ballot or {}).get("payload") or {}).get("options") or []
    code, body = call(
        "POST",
        f"/api/action-items/{did}/ballot",
        {"options": options, "message_id": "fc-ballot-open"},
        tokens[worker],
    )
    step(f"开启投票（{len(options)} 条候选）", code == 200, f"{code} {str(body.get('message'))[:50]}")
    for name in (worker, mate):
        code, body = call(
            "POST",
            f"/api/action-items/{did}/vote",
            {
                "scores": {item["option_id"]: 4 for item in options},
                "message_id": f"fc-vote-{name}",
            },
            tokens[name],
        )
        step(f"{name} 打分", code == 200, f"{code} {str(body.get('message'))[:50]}")

    # Revoking has to happen before the decision task is dispatched -- once it
    # is in flight the bindings are load-bearing -- so the throwaway structure
    # below is declared and withdrawn without ever being sent to anybody.
    state = call("GET", "/api/state?surface=manage", token=lead)[1]
    spares = [t for t in state["tasks"] if t["status"] == "PENDING_CONFIRMATION"]
    if len(spares) >= 2:
        code, _ = call(
            "POST",
            "/api/collaboration-structures/question-vote",
            {
                "collection_action_item_ids": [spares[0]["action_item_id"]],
                "decision_action_item_id": spares[1]["action_item_id"],
                "final_owner_actor_id": actors[worker],
                "voter_actor_ids": [actors[worker]],
                "selection_count": 1,
                "source_span": spares[1]["source_span"] or "transcript[0]",
                "message_id": "fc-structure-2",
            },
            lead,
        )
        step("再声明一组（用来测撤销）", code == 200, str(code))
        code, body = call(
            "POST",
            f"/api/collaboration-structures/question-vote/{spares[1]['action_item_id']}/revoke",
            {"reason": "会上改了做法", "message_id": "fc-revoke"},
            lead,
        )
        step("撤销这组结构", code == 200, f"{code} {str(body.get('message'))[:60]}")

print("\n=== 11b. 复合任务：填 → 汇总 → 投票 → 定稿 ===")

everyone = list(actors)
cmp_owner_name = everyone[1] if len(everyone) > 1 else everyone[0]
cmp_owner = actors[cmp_owner_name]

code, made = call(
    "POST",
    "/api/compound-tasks",
    {
        "kind": "VOTE",
        "title": "全链路：面试题清单",
        "body": "每人先各写几条",
        "owner_actor_id": cmp_owner,
        "member_actor_ids": [actors[name] for name in everyone],
        "selection_count": 2,
        "source_span": "00:12:04 我们各人先出几条，之后一个人汇总",
        "message_id": "cmp_chain_create",
    },
    lead,
)
step("会议负责人建复合任务", code == 200, f"{code} {str(made.get('message'))[:60]}")
compound_id = made.get("compound_task_id", "")

if compound_id:
    for index, name in enumerate(everyone):
        code, body = call(
            "POST",
            f"/api/compound-tasks/{compound_id}/input",
            {
                "payload": {"options": [f"{name} 的第一条", f"{name} 的第二条"]},
                "message_id": f"cmp_chain_fill_{index}",
            },
            tokens[name],
        )
        if index < len(everyone) - 1:
            step(
                f"{name} 填项后仍停在填写环节",
                code == 200 and not body.get("stage_complete"),
                f"{code} {body.get('stage')}",
            )
        else:
            step(
                "最后一个人填完才推进到汇总",
                code == 200 and bool(body.get("stage_complete")),
                f"{code} {body.get('stage_complete')}",
            )

    # Everybody has answered, so the stage has moved on -- a late list must not
    # land after the merge that was meant to contain it.
    code, _ = call(
        "POST",
        f"/api/compound-tasks/{compound_id}/input",
        {"payload": {"options": ["迟到的一条"]}, "message_id": "cmp_chain_late"},
        tokens[everyone[0]],
    )
    step("汇总环节里个人补交被拒", code >= 400, str(code))

    other = next(name for name in everyone if actors[name] != cmp_owner)
    code, _ = call(
        "POST",
        f"/api/compound-tasks/{compound_id}/owner-stage",
        {"payload": {"options": ["一", "二", "三"]}, "message_id": "cmp_chain_wrong"},
        tokens[other],
    )
    step("别人替负责人汇总被拒", code >= 400, str(code))

    code, _ = call(
        "POST",
        f"/api/compound-tasks/{compound_id}/owner-stage",
        {"payload": {"options": ["候选一", "候选二"]}, "message_id": "cmp_chain_thin"},
        tokens[cmp_owner_name],
    )
    step("候选条数不多于保留条数被拒", code >= 400, str(code))

    code, body = call(
        "POST",
        f"/api/compound-tasks/{compound_id}/owner-stage",
        {
            "payload": {"options": ["候选一", "候选二", "候选三", "候选四"]},
            "message_id": "cmp_chain_merge",
        },
        tokens[cmp_owner_name],
    )
    step(
        "负责人定下候选，开放投票",
        body.get("stage") == "VOTING",
        f"{code} {body.get('stage')}",
    )

    for index, name in enumerate(everyone):
        code, body = call(
            "POST",
            f"/api/compound-tasks/{compound_id}/input",
            {
                "payload": {"scores": {"0": 5, "1": 2, "2": 4, "3": 1}},
                "message_id": f"cmp_chain_vote_{index}",
            },
            tokens[name],
        )
    step(
        "所有人打完分推进到定稿",
        bool(body.get("stage_complete")),
        str(body.get("stage_complete")),
    )

    seen = call("GET", "/api/state?surface=tasks", token=tokens[everyone[0]])[1]
    row = next(
        (
            item
            for item in (seen.get("compound_tasks") or [])
            if item["compound_task_id"] == compound_id
        ),
        None,
    )
    ranking = (row or {}).get("result") or {}
    kept = [item["text"] for item in ranking.get("selected") or []]
    step(
        "排名把落选项也一并给出",
        len(ranking.get("ranked") or []) == 4 and kept == ["候选一", "候选三"],
        str(kept),
    )

    code, body = call(
        "POST",
        f"/api/compound-tasks/{compound_id}/owner-stage",
        {"payload": {"remark": "按分数留前两条"}, "message_id": "cmp_chain_final"},
        tokens[cmp_owner_name],
    )
    step("负责人定稿收尾", body.get("stage") == "DONE", f"{code} {body.get('stage')}")

code, second = call(
    "POST",
    "/api/compound-tasks",
    {
        "kind": "SUBMIT",
        "title": "全链路：材料收集",
        "body": "各自交一份",
        "owner_actor_id": cmp_owner,
        "member_actor_ids": [actors[name] for name in everyone],
        "selection_count": None,
        "source_span": "00:20:00 各自交一份材料给他汇总",
        "message_id": "cmp_chain_submit",
    },
    lead,
)
step("提交型复合任务可建", code == 200, str(code))
submit_id = second.get("compound_task_id", "")
if submit_id:
    code, _ = call(
        "POST",
        f"/api/compound-tasks/{submit_id}/revoke",
        {"reason": "", "message_id": "cmp_chain_revoke_bad"},
        tokens[cmp_owner_name],
    )
    step("撤销不写原因被拒", code >= 400, str(code))
    code, body = call(
        "POST",
        f"/api/compound-tasks/{submit_id}/revoke",
        {"reason": "人拉错了，重新建一个", "message_id": "cmp_chain_revoke"},
        tokens[cmp_owner_name],
    )
    step(
        "写了原因可以撤销",
        body.get("stage") == "REVOKED",
        f"{code} {body.get('stage')}",
    )

print("\n=== 12. 拒绝一条系统观察 ===")
rejected_one = False
for name in helpers:
    after = call("GET", "/api/state?surface=tasks", token=tokens[name])[1]
    drafts = [
        m
        for m in (after.get("memories") or [])
        if m["actor_id"] == actors[name] and m["status"] == "PRIVATE_DRAFT"
    ]
    if not drafts:
        continue
    code, _ = call(
        "POST",
        f"/api/memories/{drafts[0]['memory_id']}/reject",
        {"message_id": f"fc-mem-reject-{name}"},
        tokens[name],
    )
    step(f"{name} 拒绝一条观察", code == 200, str(code))
    rejected_one = True
    break
if not rejected_one:
    step("有草稿可拒绝", False, "没有人拿到系统观察的草稿")

print("\n=== 13. 终稿与放行 ===")
for _ in range(40):
    state = call("GET", "/api/state?surface=manage", token=lead)[1]
    pending = [
        t
        for t in state["tasks"]
        if t["required"]
        and t["status"] not in {"ACCEPTED", "AGGREGATED", "ARCHIVED", "REJECTED"}
    ]
    if not pending:
        break
    row = pending[0]
    rid = row["action_item_id"]
    tag = rid[-6:]
    if row["status"] in {"PENDING_CONFIRMATION", "NEEDS_REVISION"}:
        call(
            "POST",
            f"/api/action-items/{rid}/revise",
            {
                "title": row["title"],
                "deliverable": "补齐",
                "acceptance_criteria": "有结论",
                "priority": "P1",
                "team_required_by_sim_time": "2026-08-27T17:00:00+10:00",
                "message_id": f"fc-fin-rev-{tag}",
            },
            lead,
        )
        call(
            "POST",
            f"/api/action-items/{rid}/dispatch",
            {
                "owner_actor_id": actors[worker],
                "collaborator_actor_ids": [],
                "assignment_message": "",
                "message_id": f"fc-fin-dis-{tag}",
            },
            lead,
        )
        state = call("GET", "/api/state?surface=manage", token=lead)[1]
        row = next(t for t in state["tasks"] if t["action_item_id"] == rid)
    for assignment in row.get("current_assignments") or []:
        if assignment["response_status"] != "PENDING":
            continue
        who = next(
            (n for n, a in actors.items() if a == assignment.get("actor_id")), None
        )
        if who:
            call(
                "POST",
                f"/api/action-items/{rid}/assignment-response",
                {
                    "decision": "ACCEPT",
                    "response_message": "",
                    "message_id": f"fc-fin-acc-{tag}-{who}",
                },
                tokens[who],
            )
    state = call("GET", "/api/state?surface=manage", token=lead)[1]
    row = next(t for t in state["tasks"] if t["action_item_id"] == rid)
    if row["status"] != "PENDING_ACCEPTANCE":
        owner_name = next(
            (n for n, a in actors.items() if a == row["owner_actor_id"]), worker
        )
        call(
            "POST",
            f"/api/action-items/{rid}/submit",
            {
                "delivery": {"summary": "补交", "content": "结论一条"},
                "message_id": f"fc-fin-sub-{tag}",
            },
            tokens[owner_name],
        )
    wait_for_processing(rid)
    state = call("GET", "/api/state?surface=manage", token=lead)[1]
    row = next(t for t in state["tasks"] if t["action_item_id"] == rid)
    latest = (row.get("latest_version") or {}).get("version_id")
    if latest:
        call(
            "POST",
            f"/api/artifact-versions/{latest}/review",
            {"approve": True, "comment": "通过", "message_id": f"fc-fin-rev2-{tag}"},
            lead,
        )
    else:
        break

code, body = call("POST", "/api/final/generate", {}, lead)
step("全部验收后生成终稿", code == 200, f"{code} {str(body.get('message'))[:60]}")
state = call("GET", "/api/state?surface=manage", token=lead)[1]
approvals = state.get("pending_approvals") or []
if approvals:
    code, _ = call(
        "POST",
        f"/api/approvals/{approvals[0]['approval_id']}",
        {"approve": True, "comment": "放行"},
        lead,
    )
    step("批准放行", code == 200, str(code))
else:
    step("终稿产生了待批准项", False, "没有待批准")

print()
print("=== 14. 路由覆盖 ===")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_surface_coverage import KNOWN_UNREACHABLE, declared_routes  # noqa: E402

reachable = declared_routes() - KNOWN_UNREACHABLE
uncovered = sorted(reachable - TOUCHED)
# Counted against the intersection, not against everything touched. TOUCHED
# also holds paths that are not declared write routes (session, for one), so
# the old line could read 33/33 while naming a route it had never reached --
# a progress number that disagreed with the verdict beside it.
covered = len(reachable & TOUCHED)
step(
    f"这一趟走过 {covered}/{len(reachable)} 条写路由",
    not uncovered,
    "" if not uncovered else "没走到：" + "、".join(uncovered),
)

print()
if FAILURES:
    print(f"链路有 {len(FAILURES)} 处断开：")
    for name in FAILURES:
        print("   -", name)
    sys.exit(1)
print("全链路通过，没有断环。")
