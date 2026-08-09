import { useState } from "react";
import { messageId, postJson } from "../api";
import type { AssistanceRequest, Task } from "../manage-types";
import { Button, Chip } from "../manage/TaskCard";
import type { Act } from "./MyTaskCard";

/** Closing a help request.
 *
 *  Asking was reachable and finishing was not, which left every request open
 *  forever: the person who offered to help had no way to say it was done, and
 *  the person who asked had no way to take it back. An open request is not
 *  decoration -- it shows on the coordinator's card as an unresolved blocker,
 *  so one that can only be opened poisons the board it appears on.
 *
 *  Who may do what follows who is in it: the target acknowledges and resolves,
 *  the requester cancels. Nobody else sees the buttons.
 */
export function AssistancePanel({
  task,
  me,
  act,
}: {
  task: Task;
  me: string;
  act: Act;
}) {
  const open = (task.assistance_requests || []).filter(
    (request) => request.status === "OPEN" || request.status === "ACKNOWLEDGED",
  );
  if (!open.length) return null;
  return (
    <div className="mt-3 grid gap-2 border-t border-rule-2 pt-3">
      {open.map((request) => (
        <Row key={request.assistance_id} request={request} me={me} act={act} />
      ))}
    </div>
  );
}

function Row({
  request,
  me,
  act,
}: {
  request: AssistanceRequest;
  me: string;
  act: Act;
}) {
  const [summary, setSummary] = useState("");
  const [closing, setClosing] = useState(false);
  const mine = request.requested_by_actor_id === me;
  const target = (request as { target_actor_id?: string }).target_actor_id === me;
  const acknowledged = request.status === "ACKNOWLEDGED";

  // Literal paths, not a verb glued on: a URL built from a variable is
  // invisible to the check that every server route is reachable from a page,
  // which is exactly how these three went missing in the first place.
  const send = (url: string, body: Record<string, unknown>, done: string) =>
    void act(async () => {
      await postJson(url, { ...body, message_id: messageId("assistance") });
      setClosing(false);
      setSummary("");
    }, done);
  const id = request.assistance_id;

  return (
    <div className="rounded border border-warn bg-warn-wash px-3 py-2.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-[0.83rem]">
          {request.summary || "有人在这个任务上求助"}
        </b>
        <Chip tone="warn">{acknowledged ? "有人接手了" : "还没人接"}</Chip>
      </div>

      <div className="mt-2 flex flex-wrap gap-2">
        {target && !acknowledged ? (
          <Button
            onClick={() => send(`/api/assistance/${id}/acknowledge`, {}, "已接手，对方能看到")}
          >
            我来接手
          </Button>
        ) : null}
        {target && acknowledged ? (
          <Button tone="good" onClick={() => setClosing((open) => !open)}>
            标记解决
          </Button>
        ) : null}
        {mine ? (
          <Button
            tone="ghost"
            onClick={() => send(`/api/assistance/${id}/cancel`, {}, "已撤销这条求助")}
          >
            撤销求助
          </Button>
        ) : null}
        {!target && !mine ? (
          <span className="text-[0.78rem] text-ink-3">
            这条求助不是发给你的
          </span>
        ) : null}
      </div>

      {closing ? (
        <div className="mt-2 grid gap-2">
          <label className="grid gap-1 text-[0.79rem]">
            怎么解决的（对方会看到）
            <textarea
              rows={2}
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
              className="rounded border border-rule bg-raise px-2 py-1 text-[0.82rem]"
            />
          </label>
          <div className="flex gap-2">
            <Button
              tone="good"
              disabled={!summary.trim()}
              onClick={() =>
                send(
                  `/api/assistance/${id}/resolve`,
                  { resolution_summary: summary },
                  "已标记解决",
                )
              }
            >
              确认解决
            </Button>
            <Button tone="ghost" onClick={() => setClosing(false)}>
              取消
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
