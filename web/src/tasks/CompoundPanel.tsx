import { useState } from "react";
import { messageId, postJson } from "../api";

/** One shape the meeting decided on, as the server projects it. */
export type CompoundTask = {
  compound_task_id: string;
  kind: "VOTE" | "SUBMIT";
  title: string;
  body: string;
  stage: string;
  stage_role: "EVERYONE" | "OWNER" | "NOBODY";
  owner_actor_id: string;
  member_actor_ids: string[];
  member_count: number;
  answered_count: number;
  selection_count: number | null;
  source_span: string;
  my_turn: boolean;
  my_input: { options?: string[]; content?: string; scores?: Record<string, number> } | null;
  options: string[];
  /** Who was passed over at which stage, and why. Carried so the panel can
   *  say it rather than silently showing a smaller roster. */
  skipped: {
    actor_id: string;
    stage: string;
    reason: string;
    skipped_by_actor_id: string;
  }[];
  /** Display names for the roster, so a skip control can name a person. */
  member_names?: Record<string, string>;
  collected: { actor_id: string; payload: { options?: string[]; content?: string } }[];
  result: {
    voted_count: number;
    member_count: number;
    complete: boolean;
    selection_count: number;
    ranked: { index: number; text: string; score_total: number; score_average: number | null }[];
    selected: { index: number; text: string }[];
  } | null;
};

const STAGE_TITLES: Record<string, string> = {
  COLLECTING: "填写",
  MERGING: "汇总",
  VOTING: "投票",
  FINALIZING: "定稿",
  DONE: "已完成",
  REVOKED: "已撤销",
};

/** The order of play per kind, mirroring the server's STAGES.
 *  Drawn rather than described: somebody who can see that voting comes after
 *  a merge stops asking why their options have not appeared yet. */
const ORDER: Record<string, string[]> = {
  VOTE: ["COLLECTING", "MERGING", "VOTING", "FINALIZING", "DONE"],
  SUBMIT: ["COLLECTING", "MERGING", "DONE"],
};

function StageStrip({ task }: { task: CompoundTask }) {
  const order = ORDER[task.kind] ?? [];
  const at = order.indexOf(task.stage);
  return (
    <ol className="flex flex-wrap items-center gap-1">
      {order.map((stage, index) => {
        const done = at > index;
        const here = at === index;
        return (
          <li key={stage} className="flex items-center gap-1">
            <span
              className={
                "rounded px-1.5 py-0.5 font-mono text-[0.7rem] " +
                (here
                  ? "bg-ink text-paper"
                  : done
                    ? "text-ink-3 line-through"
                    : "text-ink-3")
              }
            >
              {STAGE_TITLES[stage] ?? stage}
            </span>
            {index < order.length - 1 ? (
              <span className="text-ink-3" aria-hidden>
                ›
              </span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** Blank rows with a ＋, which is the thing the earlier version never offered.
 *  One row per option, because somebody typing them already knows where each
 *  ends -- asking a model to recover that from a paragraph only loses it. */
function OptionRows({
  rows,
  setRows,
  label,
}: {
  rows: string[];
  setRows: (rows: string[]) => void;
  label: string;
}) {
  return (
    <div className="grid gap-1.5">
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <span className="w-5 shrink-0 text-right font-mono text-[0.7rem] text-ink-3">
            {index + 1}
          </span>
          <input
            value={row}
            aria-label={`${label} ${index + 1}`}
            onChange={(event) => {
              const next = [...rows];
              next[index] = event.target.value;
              setRows(next);
            }}
            className="min-w-0 flex-1 rounded border border-rule bg-paper px-2 py-1 text-[0.8rem]"
          />
          {rows.length > 1 ? (
            <button
              onClick={() => setRows(rows.filter((_, at) => at !== index))}
              aria-label={`删除第 ${index + 1} 行`}
              className="px-1 font-mono text-[0.8rem] text-ink-3 hover:text-ink"
            >
              ×
            </button>
          ) : null}
        </div>
      ))}
      <button
        onClick={() => setRows([...rows, ""])}
        className="justify-self-start rounded border border-dashed border-rule px-2 py-1 font-mono text-[0.75rem] text-ink-2 hover:border-ink hover:text-ink"
      >
        ＋ 再加一条
      </button>
    </div>
  );
}

export function CompoundPanel({
  task,
  me,
  reload,
}: {
  task: CompoundTask;
  me: string;
  reload: () => void;
}) {
  const [rows, setRows] = useState<string[]>(task.my_input?.options ?? [""]);
  const [content, setContent] = useState(task.my_input?.content ?? "");
  const [scores, setScores] = useState<Record<string, number>>(
    task.my_input?.scores ?? {},
  );
  const [merged, setMerged] = useState<string[]>([]);
  const [remark, setRemark] = useState("");
  const [revoking, setRevoking] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [skipTarget, setSkipTarget] = useState("");
  const [skipReason, setSkipReason] = useState("");
  const owner = me === task.owner_actor_id;
  const stageSkips = (task.skipped ?? []).filter(
    (skip) => skip.stage === task.stage,
  );
  // Who the stage is still waiting on: on the roster, has not answered, has
  // not already been passed over.
  const answered = new Set(task.collected.map((entry) => entry.actor_id));
  const waiting = task.member_actor_ids.filter(
    (actorId) =>
      actorId !== task.owner_actor_id &&
      !answered.has(actorId) &&
      !stageSkips.some((skip) => skip.actor_id === actorId),
  );

  /** Each operation spells its own URL out.
   *
   *  Building the path from a variable would read fine and typecheck, but it
   *  hides which routes a page actually calls -- and the guard that checks
   *  every server route is reachable from some page then cannot prove any of
   *  the three. Naming them is what makes that check mean something.
   */
  async function send(
    call: () => Promise<unknown>,
  ) {
    setBusy(true);
    setError("");
    try {
      await call();
      reload();
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const key = (verb: string) =>
    messageId(`cmp_${verb}_${task.compound_task_id}_${me}`);

  const sendInput = (payload: Record<string, unknown>) =>
    send(() =>
      postJson(`/api/compound-tasks/${task.compound_task_id}/input`, {
        payload,
        message_id: key("input"),
      }),
    );

  const sendOwnerStage = (payload: Record<string, unknown>) =>
    send(() =>
      postJson(`/api/compound-tasks/${task.compound_task_id}/owner-stage`, {
        payload,
        message_id: key("owner"),
      }),
    );

  const sendRevoke = (reason: string) =>
    send(() =>
      postJson(`/api/compound-tasks/${task.compound_task_id}/revoke`, {
        reason,
        message_id: key("revoke"),
      }),
    );

  const candidates = task.collected.flatMap((entry) => entry.payload.options ?? []);

  return (
    <article className="rounded-lg border border-rule bg-paper-2 p-3">
      <header className="grid gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-[0.95rem] font-semibold text-ink">{task.title}</h3>
          <span className="font-mono text-[0.7rem] text-ink-3">
            {task.kind === "VOTE" ? "投票型" : "提交型"} ·{" "}
            {task.member_count} 人
          </span>
        </div>
        {task.body ? (
          <p className="text-[0.8rem] leading-relaxed text-ink-2">{task.body}</p>
        ) : null}
        <StageStrip task={task} />
        {/* Where the meeting said this, kept visible: a shape nobody remembers
            agreeing to is the one that gets ignored. */}
        <p className="font-mono text-[0.7rem] text-ink-3">出处 {task.source_span}</p>
      </header>

      <div className="mt-3 grid gap-2 border-t border-rule pt-3">
        {task.stage_role === "EVERYONE" ? (
          <p className="font-mono text-[0.75rem] text-ink-3">
            已交 {task.answered_count} / {task.member_count}
            {task.answered_count < task.member_count
              ? " · 要等所有人交齐才进下一环节"
              : ""}
          </p>
        ) : null}

        {stageSkips.length ? (
          <div className="rounded border border-rule-2 bg-sunk px-3 py-2 text-[0.78rem] text-ink-2">
            {stageSkips.map((skip) => (
              <p key={skip.actor_id}>
                跳过了 <b>{task.member_names?.[skip.actor_id] ?? skip.actor_id}</b>
                ：{skip.reason}
              </p>
            ))}
          </div>
        ) : null}

        {owner && task.stage_role === "EVERYONE" && waiting.length ? (
          <div className="rounded border border-dashed border-rule-2 px-3 py-2.5">
            <p className="text-[0.79rem] text-ink-2">
              还差 {waiting.length} 个人没交。等是默认的——一份少了一个人的清单，
              后面没人看得出来少了谁。确实等不到的话可以跳过，会记下是你跳的和原因。
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <select
                value={skipTarget}
                onChange={(event) => setSkipTarget(event.target.value)}
                className="rounded border border-rule bg-raise px-2 py-1 text-[0.8rem]"
              >
                <option value="">选一个人</option>
                {waiting.map((actorId) => (
                  <option key={actorId} value={actorId}>
                    {task.member_names?.[actorId] ?? actorId}
                  </option>
                ))}
              </select>
              <input
                value={skipReason}
                onChange={(event) => setSkipReason(event.target.value)}
                placeholder="为什么不等了"
                className="min-w-[12rem] flex-1 rounded border border-rule bg-raise px-2 py-1 text-[0.8rem]"
              />
              <button
                disabled={!skipTarget || !skipReason.trim() || busy}
                onClick={() => {
                  setBusy(true);
                  void postJson(
                    `/api/compound-tasks/${task.compound_task_id}/skip`,
                    {
                      target_actor_id: skipTarget,
                      reason: skipReason,
                      message_id: messageId("compound-skip"),
                    },
                  )
                    .then(() => {
                      setSkipTarget("");
                      setSkipReason("");
                      reload();
                    })
                    .finally(() => setBusy(false));
                }}
                className="rounded border border-rule px-2.5 py-1 text-[0.8rem] hover:bg-ground disabled:opacity-40"
              >
                跳过并继续
              </button>
            </div>
          </div>
        ) : null}

        {!task.my_turn ? (
          <p className="text-[0.8rem] text-ink-2">
            {task.stage === "DONE"
              ? "这项已经完成。"
              : task.stage === "REVOKED"
                ? "这项已被撤销。"
                : task.stage_role === "OWNER"
                  ? "轮到负责人处理，等结果就行。"
                  : "你这一环节已经交过了，等其他人。"}
          </p>
        ) : task.stage === "COLLECTING" ? (
          task.kind === "VOTE" ? (
            <div className="grid gap-2">
              <p className="text-[0.8rem] text-ink-2">写下你想提的条目，一行一条。</p>
              <OptionRows rows={rows} setRows={setRows} label="条目" />
              <button
                disabled={busy}
                onClick={() =>
                  sendInput({ options: rows.filter((row) => row.trim()) })
                }
                className="justify-self-start rounded bg-ink px-3 py-1.5 text-[0.8rem] text-paper disabled:opacity-50"
              >
                提交我的条目
              </button>
            </div>
          ) : (
            <div className="grid gap-2">
              <textarea
                value={content}
                aria-label="我的内容"
                rows={4}
                onChange={(event) => setContent(event.target.value)}
                className="rounded border border-rule bg-paper px-2 py-1.5 text-[0.8rem]"
              />
              <button
                disabled={busy}
                onClick={() => sendInput({ content })}
                className="justify-self-start rounded bg-ink px-3 py-1.5 text-[0.8rem] text-paper disabled:opacity-50"
              >
                提交
              </button>
            </div>
          )
        ) : task.stage === "MERGING" && owner ? (
          <div className="grid gap-2">
            <p className="text-[0.8rem] text-ink-2">
              {task.member_count} 个人交上来的条目在下面。挑进候选清单，可以改写、也可以自己加。
              {task.kind === "VOTE" && task.selection_count
                ? ` 最后保留 ${task.selection_count} 条，所以候选要多于 ${task.selection_count} 条。`
                : ""}
            </p>
            {candidates.length ? (
              <ul className="grid gap-1">
                {candidates.map((text, index) => (
                  <li key={index} className="flex items-start gap-2">
                    <button
                      onClick={() => setMerged([...merged, text])}
                      className="mt-0.5 shrink-0 rounded border border-rule px-1.5 font-mono text-[0.7rem] text-ink-2 hover:border-ink hover:text-ink"
                    >
                      ＋
                    </button>
                    <span className="text-[0.8rem] text-ink-2">{text}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[0.8rem] text-ink-3">
                {task.collected
                  .map((entry) => entry.payload.content)
                  .filter(Boolean)
                  .join("\n\n") || "没有收到内容。"}
              </p>
            )}
            <div className="border-t border-rule pt-2">
              <p className="mb-1.5 font-mono text-[0.75rem] text-ink-3">候选清单</p>
              {task.kind === "VOTE" ? (
                <OptionRows
                  rows={merged.length ? merged : [""]}
                  setRows={setMerged}
                  label="候选"
                />
              ) : (
                <textarea
                  value={content}
                  aria-label="汇总稿"
                  rows={5}
                  onChange={(event) => setContent(event.target.value)}
                  className="w-full rounded border border-rule bg-paper px-2 py-1.5 text-[0.8rem]"
                />
              )}
            </div>
            <button
              disabled={busy}
              onClick={() =>
                sendOwnerStage(
                  task.kind === "VOTE"
                    ? { options: merged.filter((row) => row.trim()) }
                    : { content },
                )
              }
              className="justify-self-start rounded bg-ink px-3 py-1.5 text-[0.8rem] text-paper disabled:opacity-50"
            >
              {task.kind === "VOTE" ? "定下候选，开放投票" : "提交汇总稿"}
            </button>
          </div>
        ) : task.stage === "VOTING" ? (
          <div className="grid gap-2">
            <p className="text-[0.8rem] text-ink-2">每条打 1 到 5 分，全部打完才能提交。</p>
            <ul className="grid gap-1.5">
              {task.options.map((text, index) => (
                <li key={index} className="flex items-center justify-between gap-3">
                  <span className="min-w-0 flex-1 text-[0.8rem] text-ink-2">{text}</span>
                  <span className="flex shrink-0 gap-1">
                    {[1, 2, 3, 4, 5].map((score) => (
                      <button
                        key={score}
                        aria-label={`第 ${index + 1} 条给 ${score} 分`}
                        aria-pressed={scores[String(index)] === score}
                        onClick={() =>
                          setScores({ ...scores, [String(index)]: score })
                        }
                        className={
                          "h-6 w-6 rounded font-mono text-[0.7rem] " +
                          (scores[String(index)] === score
                            ? "bg-ink text-paper"
                            : "border border-rule text-ink-3 hover:border-ink")
                        }
                      >
                        {score}
                      </button>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
            <button
              disabled={busy || Object.keys(scores).length < task.options.length}
              onClick={() => sendInput({ scores })}
              className="justify-self-start rounded bg-ink px-3 py-1.5 text-[0.8rem] text-paper disabled:opacity-50"
            >
              提交打分
            </button>
          </div>
        ) : task.stage === "FINALIZING" && owner ? (
          <div className="grid gap-2">
            <p className="text-[0.8rem] text-ink-2">
              {task.result
                ? `${task.result.voted_count} 个人打完分了。按分数排在前 ${task.result.selection_count} 的是下面加粗的几条。`
                : ""}
            </p>
            <input
              value={remark}
              aria-label="定稿备注"
              placeholder="定稿备注（可留空）"
              onChange={(event) => setRemark(event.target.value)}
              className="rounded border border-rule bg-paper px-2 py-1.5 text-[0.8rem]"
            />
            <button
              disabled={busy}
              onClick={() => sendOwnerStage({ remark })}
              className="justify-self-start rounded bg-ink px-3 py-1.5 text-[0.8rem] text-paper disabled:opacity-50"
            >
              定稿，交给会议负责人
            </button>
          </div>
        ) : null}

        {/* The whole ranking, not only the survivors: which options came close
            is how somebody judges whether the cut was drawn sensibly. */}
        {task.result && task.result.ranked.length ? (
          <ol className="mt-1 grid gap-1 border-t border-rule pt-2">
            {task.result.ranked.map((entry, place) => (
              <li
                key={entry.index}
                className="flex items-baseline justify-between gap-3"
              >
                <span
                  className={
                    "min-w-0 flex-1 text-[0.8rem] " +
                    (place < task.result!.selection_count
                      ? "font-semibold text-ink"
                      : "text-ink-3")
                  }
                >
                  {entry.text}
                </span>
                <span className="shrink-0 font-mono text-[0.7rem] tabular-nums text-ink-3">
                  {entry.score_total} 分
                  {entry.score_average !== null
                    ? ` · 均 ${entry.score_average}`
                    : ""}
                </span>
              </li>
            ))}
          </ol>
        ) : null}

        {/* Withdrawing is the owner's, and it costs a sentence. A shape
            declared over the wrong people wastes five people's time, so it
            should be undoable -- but everybody already asked to fill it in
            deserves to know why it went away. */}
        {owner && task.stage !== "DONE" && task.stage !== "REVOKED" ? (
          revoking ? (
            <div className="flex flex-wrap items-center gap-2 border-t border-rule pt-2">
              <input
                value={reason}
                aria-label="撤销原因"
                placeholder="撤销原因，参与的人会看到"
                onChange={(event) => setReason(event.target.value)}
                className="min-w-0 flex-1 rounded border border-rule bg-paper px-2 py-1 text-[0.8rem]"
              />
              <button
                disabled={busy || !reason.trim()}
                onClick={() => sendRevoke(reason)}
                className="rounded border border-rose-700 px-2.5 py-1 text-[0.78rem] text-rose-700 disabled:opacity-40"
              >
                确认撤销
              </button>
              <button
                onClick={() => setRevoking(false)}
                className="text-[0.78rem] text-ink-3 underline"
              >
                算了
              </button>
            </div>
          ) : (
            <button
              onClick={() => setRevoking(true)}
              className="justify-self-start font-mono text-[0.72rem] text-ink-3 underline hover:text-ink"
            >
              撤销这项
            </button>
          )
        ) : null}

        {error ? (
          <p className="text-[0.8rem] text-rose-700" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </article>
  );
}
