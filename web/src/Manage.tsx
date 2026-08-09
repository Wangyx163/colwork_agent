import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, messageId, postJson } from "./api";
import type { ManageState, Task } from "./manage-types";
import { buildStrip, formatDay } from "./manage/schedule";
import { ScheduleStrip } from "./manage/ScheduleStrip";
import { StructurePanel } from "./manage/StructurePanel";
import { Blank, Zone } from "./manage/Zone";
import { Button, Chip, TaskCard, type CardAction } from "./manage/TaskCard";

const IN_FLIGHT = new Set(["TRACKING", "BLOCKED"]);
const AWAITING_DISPATCH = new Set([
  "PENDING_CONFIRMATION",
  "PENDING_ASSIGNMENT",
  "NEEDS_REVISION",
]);
const DONE = new Set(["ACCEPTED", "AGGREGATED", "ARCHIVED"]);

export default function ManagePage() {
  const [state, setState] = useState<ManageState | null>(null);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [reviewHistory, setReviewHistory] = useState(false);
  const cards = useRef(new Map<string, HTMLElement>());

  const load = useCallback(async () => {
    try {
      setState(await getJson<ManageState>("/api/state?surface=manage"));
      setError("");
    } catch (problem) {
      setError((problem as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (run: () => Promise<unknown>, done: string) => {
      try {
        await run();
        setFlash(done);
        await load();
      } catch (problem) {
        setFlash((problem as Error).message);
      }
    },
    [load],
  );

  const tasks = state?.tasks ?? [];

  // Every task that has a schedule to speak of, completed ones included: the
  // strip is the index of the whole page, so a row exists for anything a
  // reader might then want to jump to.
  const strip = useMemo(
    () =>
      buildStrip(
        tasks.filter(
          (task) =>
            task.status !== "REJECTED" && task.team_required_by_sim_time,
        ),
        state?.episode?.current_sim_time as string | undefined,
      ),
    [tasks, state],
  );

  const lateness = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of strip.rows)
      if (row.late) map.set(row.task.action_item_id, row.lateDays);
    return map;
  }, [strip]);

  /** Selecting from the strip promotes the card and outlines it. Nothing
   *  collapses and nothing pops: rows a reader was already comparing stay
   *  exactly where they were. */
  const pick = useCallback((id: string) => {
    setSelected(id);
    const node = cards.current.get(id);
    if (node)
      node.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  const bind = useCallback(
    (id: string) => (node: HTMLElement | null) => {
      if (node) cards.current.set(id, node);
      else cards.current.delete(id);
    },
    [],
  );

  if (error)
    return (
      <main className="mx-auto max-w-2xl p-8">
        <h1 className="text-xl font-bold">打不开工作台</h1>
        <p className="mt-3 text-ink-2">{error}</p>
        <p className="mt-3 text-[0.88rem] text-ink-3">
          这个页面只对会议负责人开放。请先在
          <a className="text-accent underline" href="/tasks">
            {" "}
            工作台{" "}
          </a>
          选择负责人身份，再回到这里。
        </p>
      </main>
    );

  if (!state)
    return <main className="mx-auto max-w-2xl p-8 text-ink-3">正在读取…</main>;

  const inFlight = tasks.filter((task) => IN_FLIGHT.has(task.status));
  const awaiting = tasks.filter((task) => AWAITING_DISPATCH.has(task.status));
  const toReview = tasks.filter((task) => task.status === "PENDING_ACCEPTANCE");
  const reviewed = tasks.filter((task) => DONE.has(task.status));
  const conflicts = inFlight.filter((task) =>
    lateness.has(task.action_item_id),
  ).length;

  return (
    <div className="mx-auto max-w-[74rem] px-4 pb-20 sm:px-8">
      <header className="mb-6 flex flex-wrap items-baseline gap-x-6 gap-y-2 border-b border-rule py-7">
        <h1 className="text-xl font-bold tracking-tight sm:text-2xl">
          会议工作台
        </h1>
        <span className="tabular font-mono text-[0.78rem] text-ink-2">
          {String(state.episode.title ?? state.episode.episode_id)} ·{" "}
          {state.meeting_progress.total} 个任务 · 现在{" "}
          {formatDay(state.episode.current_sim_time as string)}
        </span>
        <nav className="ml-auto flex gap-4 font-mono text-[0.75rem]">
          <a className="text-accent underline" href="/tasks">
            我的任务
          </a>
          <a className="text-accent underline" href="/observatory">
            Observatory
          </a>
        </nav>
      </header>

      {flash ? (
        <p
          role="status"
          onClick={() => setFlash("")}
          className="mb-4 cursor-pointer rounded border border-rule bg-accent-wash px-4 py-2 text-[0.85rem]"
        >
          {flash}
        </p>
      ) : null}

      {/* 01 --------------------------------------------------------------- */}
      <Zone
        n="01"
        name="团队进度"
        anchor="zone-tasks"
        pending={conflicts}
        pendingLabel={
          conflicts ? `${conflicts} 项工期冲突` : `${inFlight.length} 项执行中`
        }
        why="每根条是团队留给这件事的时间，右端那道竖杠是团队要求交付的那天。虚线是本人承诺比它还晚的部分。点任一条，下面对应的卡片会高亮。"
      >
        <ScheduleStrip strip={strip} selected={selected} onPick={pick} />
        {inFlight.length ? (
          <div className="grid gap-2">
            {inFlight.map((task) => (
              <InFlightCard
                key={task.action_item_id}
                task={task}
                me={state.principal.actor_id}
                late={lateness.has(task.action_item_id)}
                lateDays={lateness.get(task.action_item_id)}
                selected={selected === task.action_item_id}
                cardRef={bind(task.action_item_id)}
                onSelect={() => setSelected(task.action_item_id)}
                act={act}
              />
            ))}
          </div>
        ) : (
          <Blank>没有正在执行的任务。</Blank>
        )}
      </Zone>

      {/* 02 --------------------------------------------------------------- */}
      <Zone
        n="02"
        name="派发"
        anchor="zone-dispatch"
        pending={awaiting.length}
        pendingLabel={`${awaiting.length} 项待派发`}
        ownerOnly
        why="抽取出来但还没派出去的任务。主负责人和协作者都在这里一次定下——任务负责人事后不能再拉人，只能向指定的人求助。全部被派到的人都接受，任务才进入执行。"
      >
        {awaiting.length ? (
          <div className="grid gap-2">
            {awaiting.map((task) => (
              <DispatchRow
                key={task.action_item_id}
                task={task}
                state={state}
                act={act}
                selected={selected === task.action_item_id}
                cardRef={bind(task.action_item_id)}
              />
            ))}
          </div>
        ) : (
          <Blank>都派完了。</Blank>
        )}
      </Zone>

      <StructurePanel state={state} act={act} />

      {/* 03 --------------------------------------------------------------- */}
      <Zone
        n="03"
        name="验收"
        anchor="zone-review"
        pending={toReview.length}
        pendingLabel={
          reviewHistory
            ? `已完成 ${reviewed.length} 项`
            : `${toReview.length} 项待验收`
        }
        why="跟 01 是同一套卡片语言，但没有排期条——验收看的是内容不是时间。验收完的收进顶部的历史，不留在这里堆着。"
      >
        {reviewHistory ? (
          reviewed.length ? (
            <div className="grid gap-2">
              {reviewed.map((task) => (
                <TaskCard
                  key={task.action_item_id}
                  task={task}
                  selected={selected === task.action_item_id}
                  cardRef={bind(task.action_item_id)}
                  actions={[]}
                />
              ))}
            </div>
          ) : (
            <Blank>还没有验收完成的任务。</Blank>
          )
        ) : toReview.length ? (
          <div className="grid gap-2">
            {toReview.map((task) => (
              <ReviewCard
                key={task.action_item_id}
                task={task}
                act={act}
                selected={selected === task.action_item_id}
                cardRef={bind(task.action_item_id)}
              />
            ))}
          </div>
        ) : (
          <Blank>没有待验收的成果。</Blank>
        )}

        {/* Inside the zone, in the same place the participant page puts it.
            At the top of the console it read as a page-wide mode switch while
            it only ever changed this one list. */}
        <div className="mt-3 text-right">
          <button
            onClick={() => setReviewHistory((open) => !open)}
            aria-pressed={reviewHistory}
            className="font-mono text-[0.75rem] text-ink-3 underline hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            {reviewHistory
              ? "← 返回待验收"
              : `🕐 看已验收 ${reviewed.length} 项`}
          </button>
        </div>
      </Zone>

      {/* 04 --------------------------------------------------------------- */}
      <FinalZone state={state} act={act} />
    </div>
  );
}

/* ------------------------------------------------------------------ 01 */

type Act = (run: () => Promise<unknown>, done: string) => Promise<void>;

/** A task being worked on.
 *
 *  The only thing changeable from here is what the task *says*, and only by
 *  the person doing it. Who owns it, who helps, and when it is due were
 *  settled at dispatch and accepted by name; the console does not get to
 *  reopen that quietly. Anyone else looking at a task they cannot amend sees
 *  no button rather than a disabled one -- a control that is never usable is
 *  just a question mark.
 */
function InFlightCard({
  task,
  me,
  late,
  lateDays,
  selected,
  cardRef,
  onSelect,
  act,
}: {
  task: Task;
  me: string;
  late: boolean;
  lateDays?: number;
  selected: boolean;
  cardRef: (node: HTMLElement | null) => void;
  onSelect: () => void;
  act: Act;
}) {
  const meta = (task.proposal_metadata || {}) as Record<string, string>;
  const mine = task.owner_actor_id === me;
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const [deliverable, setDeliverable] = useState(meta.deliverable || "");

  const actions: CardAction[] = mine
    ? [
        {
          label: editing ? "收起" : "修改任务说明",
          tone: "ghost",
          run: () => setEditing((open) => !open),
        },
      ]
    : [];

  const save = () =>
    void act(async () => {
      await postJson(`/api/action-items/${task.action_item_id}/amend`, {
        title,
        deliverable,
        message_id: messageId("amend"),
      });
      setEditing(false);
    }, "已更新，正在协作的人会收到提示");

  return (
    <TaskCard
      task={task}
      late={late}
      lateDays={lateDays}
      selected={selected}
      cardRef={cardRef}
      onSelect={onSelect}
      actions={actions}
      extra={
        editing ? (
          <div className="mt-3 grid gap-2.5 border-t border-rule-2 pt-3">
            <p className="text-[0.79rem] text-ink-3">
              只改名称和说明。负责人、协作者、团队要求交付的时间都不会动——那是派发时定下、对方接受过的。
            </p>
            <label className="grid gap-1 text-[0.79rem]">
              任务名称
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className="rounded border border-rule bg-ground px-2 py-1 text-[0.82rem]"
              />
            </label>
            <label className="grid gap-1 text-[0.79rem]">
              任务说明
              <textarea
                rows={3}
                value={deliverable}
                onChange={(event) => setDeliverable(event.target.value)}
                className="rounded border border-rule bg-ground px-2 py-1 text-[0.82rem]"
              />
            </label>
            <div className="flex gap-2">
              <Button
                onClick={save}
                disabled={!title.trim() || !deliverable.trim()}
              >
                保存并通知
              </Button>
              <Button tone="ghost" onClick={() => setEditing(false)}>
                取消
              </Button>
            </div>
          </div>
        ) : null
      }
    />
  );
}

/* ------------------------------------------------------------------ 02 */

function DispatchRow({
  task,
  state,
  act,
  selected,
  cardRef,
}: {
  task: Task;
  state: ManageState;
  act: Act;
  selected: boolean;
  cardRef: (node: HTMLElement | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [owner, setOwner] = useState(task.owner_actor_id || "");
  const [helpers, setHelpers] = useState<string[]>([]);
  const [when, setWhen] = useState(
    (task.team_required_by_sim_time || "").slice(0, 10),
  );
  const [message, setMessage] = useState("");

  const send = () => {
    if (!owner) return;
    void act(async () => {
      if (when)
        await postJson(`/api/action-items/${task.action_item_id}/revise`, {
          title: task.title,
          deliverable:
            (task.proposal_metadata as Record<string, string>)?.deliverable ||
            task.deliverable_key,
          acceptance_criteria:
            (task.proposal_metadata as Record<string, string>)
              ?.acceptance_criteria || "",
          priority: "P1",
          team_required_by_sim_time: `${when}T17:00:00+10:00`,
          message_id: messageId("revise"),
        });
      await postJson(`/api/action-items/${task.action_item_id}/dispatch`, {
        owner_actor_id: owner,
        collaborator_actor_ids: helpers.filter((id) => id !== owner),
        assignment_message: message,
        message_id: messageId("dispatch"),
      });
    }, "已派发，等被派到的人全部接受");
  };

  return (
    <article
      ref={cardRef}
      className={`rounded-md border bg-raise px-3.5 py-3 ${
        selected ? "border-accent ring-2 ring-accent-wash" : "border-rule-2"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[0.9rem] font-semibold">{task.title}</span>
        {task.status === "NEEDS_REVISION" ? (
          <Chip tone="warn">被退回，需重新派发</Chip>
        ) : (
          <Chip>待派发</Chip>
        )}
      </div>
      <div className="tabular mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[0.71rem] text-ink-3">
        <span>
          会议出处 <b className="font-semibold text-ink-2">{task.source_span || "—"}</b>
        </span>
        <span>团队要求交付 {formatDay(task.team_required_by_sim_time)}</span>
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <Button onClick={() => setOpen((value) => !value)}>
          {open ? "收起" : "复核并派发"}
        </Button>
      </div>

      {open ? (
        <div className="mt-3 grid gap-2.5 border-t border-rule-2 pt-3">
          <label className="grid gap-1 text-[0.79rem]">
            主负责人
            <select
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              className="rounded border border-rule bg-ground px-2 py-1 text-[0.82rem]"
            >
              <option value="">请选择</option>
              {state.participants.map((person) => (
                <option key={person.actor_id} value={person.actor_id}>
                  {person.display_name}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="grid gap-1 text-[0.79rem]">
            <legend className="mb-1">协作者（可多选，事后不能再加）</legend>
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {state.participants
                .filter((person) => person.actor_id !== owner)
                .map((person) => (
                  <label
                    key={person.actor_id}
                    className="inline-flex items-center gap-1.5 text-[0.8rem]"
                  >
                    <input
                      type="checkbox"
                      checked={helpers.includes(person.actor_id)}
                      onChange={(event) =>
                        setHelpers((current) =>
                          event.target.checked
                            ? [...current, person.actor_id]
                            : current.filter((id) => id !== person.actor_id),
                        )
                      }
                    />
                    {person.display_name}
                  </label>
                ))}
            </div>
          </fieldset>
          <label className="grid gap-1 text-[0.79rem]">
            团队要求交付
            <input
              type="date"
              value={when}
              onChange={(event) => setWhen(event.target.value)}
              className="rounded border border-rule bg-ground px-2 py-1 text-[0.82rem]"
            />
            <span className="text-[0.74rem] text-ink-3">
              团队需要它在哪天之前完成。对方接受时会自己给一个承诺时间，两者不一致就是工期冲突。
            </span>
          </label>
          <label className="grid gap-1 text-[0.79rem]">
            派发说明（可选）
            <textarea
              rows={2}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              className="rounded border border-rule bg-ground px-2 py-1 text-[0.82rem]"
            />
          </label>
          <div>
            <Button onClick={send} disabled={!owner}>
              确认派发
            </Button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

/* ------------------------------------------------------------------ 03 */

function ReviewCard({
  task,
  act,
  selected,
  cardRef,
}: {
  task: Task;
  act: Act;
  selected: boolean;
  cardRef: (node: HTMLElement | null) => void;
}) {
  const [returning, setReturning] = useState(false);
  const [comment, setComment] = useState("");
  const version = task.latest_version || task.current_version;

  const decide = (approve: boolean) => {
    if (!version) return;
    void act(
      () =>
        postJson(`/api/artifact-versions/${version.version_id}/review`, {
          approve,
          comment,
          message_id: messageId("review"),
        }),
      approve ? "验收通过，单任务结果已冻结" : "已退回负责人修改",
    );
    setReturning(false);
    setComment("");
  };

  return (
    <div className="grid gap-0">
      <TaskCard
        task={task}
        selected={selected}
        cardRef={cardRef}
        actions={[
          { label: "验收通过", tone: "good", run: () => decide(true) },
          {
            label: "退回并说明",
            tone: "ghost",
            run: () => setReturning((open) => !open),
          },
        ]}
      />
      {returning ? (
        <div className="mt-1 rounded-md border border-warn bg-warn-wash px-3.5 py-3">
          <label className="grid gap-1 text-[0.79rem]">
            退回原因（对方会看到）
            <textarea
              rows={2}
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              className="rounded border border-rule bg-raise px-2 py-1 text-[0.82rem]"
            />
          </label>
          <div className="mt-2 flex gap-2">
            <Button
              tone="accent"
              disabled={!comment.trim()}
              onClick={() => decide(false)}
            >
              确认退回
            </Button>
            <Button tone="ghost" onClick={() => setReturning(false)}>
              取消
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 04 */

function FinalZone({ state, act }: { state: ManageState; act: Act }) {
  const [comment, setComment] = useState("");
  const pending = state.pending_approvals ?? [];
  const remaining = state.tasks.filter(
    (task) =>
      task.required &&
      !DONE.has(task.status) &&
      task.status !== "REJECTED",
  );
  const final = state.final;
  const released = final?.status === "RELEASED";

  return (
    <Zone
      n="04"
      name="终稿"
      anchor="zone-final"
      pending={pending.length}
      pendingLabel={
        released
          ? "已放行"
          : pending.length
            ? `${pending.length} 项等你批准`
            : remaining.length
              ? `等 ${remaining.length} 项验收完成`
              : "可以生成"
      }
      why="全部必需任务验收后才汇总。生成之后是一张带来源版本号的报告，放行需要单独一次批准——生成不等于发布。"
    >
      <div className="overflow-hidden rounded-md border border-rule-2">
        <h3 className="border-b border-rule-2 bg-sunk px-3 py-2 text-[0.82rem] font-semibold">
          {released ? "已放行的终稿" : "将汇总（预览）"}
        </h3>
        {state.tasks
          .filter((task) => task.required && task.status !== "REJECTED")
          .map((task) => (
            <div
              key={task.action_item_id}
              className="flex justify-between gap-3 border-b border-rule-2 px-3 py-2 text-[0.8rem] last:border-b-0"
            >
              <span>{task.title}</span>
              <span className="tabular font-mono text-[0.7rem] text-ink-3">
                {DONE.has(task.status) ? "已验收 ✓" : "未验收"}
              </span>
            </div>
          ))}
      </div>

      {pending.length ? (
        <div className="mt-3 grid gap-2">
          {pending.map((approval) => (
            <div
              key={approval.approval_id}
              className="rounded-md border border-accent bg-accent-wash px-3.5 py-3"
            >
              <p className="text-[0.85rem] font-semibold">
                {approval.summary || "终稿等待批准"}
              </p>
              <label className="mt-2 grid gap-1 text-[0.79rem]">
                说明（驳回时必填）
                <textarea
                  rows={2}
                  value={comment}
                  onChange={(event) => setComment(event.target.value)}
                  className="rounded border border-rule bg-raise px-2 py-1 text-[0.82rem]"
                />
              </label>
              <div className="mt-2 flex gap-2">
                <Button
                  tone="good"
                  onClick={() =>
                    void act(
                      () =>
                        postJson(`/api/approvals/${approval.approval_id}`, {
                          approve: true,
                          comment,
                        }),
                      "已批准并进入发布",
                    )
                  }
                >
                  批准放行
                </Button>
                <Button
                  tone="ghost"
                  disabled={!comment.trim()}
                  onClick={() =>
                    void act(
                      () =>
                        postJson(`/api/approvals/${approval.approval_id}`, {
                          approve: false,
                          comment,
                        }),
                      "已驳回，终稿不会发布",
                    )
                  }
                >
                  驳回
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-3 text-[0.8rem] text-ink-3">
          {released
            ? `第 ${final.revision_no} 版已于 ${formatDay(final.approved_sim_time)} 放行。`
            : remaining.length
              ? `还有 ${remaining.length} 项必需任务没验收完。`
              : "所有必需任务已验收，可以生成了。"}
        </p>
      )}

      <div className="mt-5 flex flex-col items-center gap-1.5">
        <Button
          disabled={remaining.length > 0}
          onClick={() =>
            void act(
              () => postJson("/api/final/generate", {}),
              "验收报告已生成，等你批准放行",
            )
          }
        >
          生成验收报告
        </Button>
        <span className="text-[0.75rem] text-ink-3">
          {remaining.length
            ? `${state.tasks.filter((task) => task.required && task.status !== "REJECTED").length} 项必需任务全部验收后才能生成，还差 ${remaining.length} 项`
            : "汇总每项任务的最新有效版本，生成后仍需一次批准才发布"}
        </span>
      </div>
    </Zone>
  );
}
