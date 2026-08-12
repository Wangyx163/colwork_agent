import { useState } from "react";
import { messageId, postJson } from "../api";
import type { ManageState } from "../manage-types";
import { Zone } from "./Zone";

type Act = (run: () => Promise<unknown>, done: string) => Promise<void>;

/** Declaring a compound task: a shape the meeting decided on, not a set of
 *  ordinary tasks wired together.
 *
 *  Two things are settled here and nowhere else, because both are cheap to
 *  get right in a form and expensive to recover afterwards. The kind decides
 *  whether there is a round where everybody scores what was merged -- guessing
 *  it wrong means either asking people to score nothing or never asking them
 *  at all. And the owner is the person the meeting left holding the merge; the
 *  extraction can propose them from where the headcount drops, but a person
 *  confirms it, because the whole thing lands on that one person's plate.
 */
export function CompoundDeclare({
  state,
  act,
}: {
  state: ManageState;
  act: Act;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"VOTE" | "SUBMIT">("VOTE");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [owner, setOwner] = useState("");
  const [keep, setKeep] = useState(8);
  const [span, setSpan] = useState("");

  const declared = state.compound_tasks ?? [];
  const people = state.participants ?? [];

  const send = () =>
    void act(async () => {
      await postJson("/api/compound-tasks", {
        kind,
        title,
        body,
        owner_actor_id: owner,
        member_actor_ids: members,
        selection_count: kind === "VOTE" ? keep : null,
        source_span: span,
        message_id: messageId("compound"),
      });
      setOpen(false);
      setTitle("");
      setBody("");
      setMembers([]);
      setOwner("");
      setSpan("");
    }, "复合任务已建，参与的人在「我的任务」里就能看到");

  // The owner has to be one of the members: merging without having
  // contributed is how a summary drifts from what people wrote.
  const ready =
    title.trim() &&
    span.trim() &&
    members.length >= 2 &&
    owner &&
    members.includes(owner);

  return (
    <Zone
      n=""
      tool
      // The section is named for the thing, not the act. Naming it "建一个
      // 复合任务" put the same six characters on the heading and on the button
      // under it, which reads as two controls until you try both.
      name="复合任务"
      anchor="zone-compound"
      pending={declared.filter((task) => task.stage !== "DONE").length}
      pendingLabel={`${declared.length} 项已建`}
      ownerOnly
      action={
        open ? null : (
          <button
            onClick={() => setOpen(true)}
            className="rounded border border-rule px-2.5 py-1 font-mono text-[0.76rem] text-ink-2 hover:border-ink hover:text-ink"
          >
            ＋ 建一个
          </button>
        )
      }
    >
      {declared.length ? (
        <ul className="grid gap-1.5">
          {declared.map((task) => (
            <li
              key={task.compound_task_id}
              className="rounded border border-rule-2 bg-raise px-3 py-2.5"
            >
              <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
                <span className="text-[0.85rem] font-medium text-ink">
                  {task.title}
                </span>
                <span className="rounded-full bg-sunk px-2 py-px font-mono text-[0.7rem] text-ink-3">
                  {task.kind === "VOTE" ? "投票型" : "提交型"}
                </span>
                <span className="tabular ml-auto font-mono text-[0.7rem] text-ink-3">
                  {task.answered_count}/{task.member_count} 已交
                </span>
              </div>
              {/* The stage track is the whole reason this is not a task card:
                  a task has one state, this has an order of play, and where it
                  has got to is the only thing a reader wants at a glance. */}
              <StageTrack kind={task.kind} stage={task.stage} />
            </li>
          ))}
        </ul>
      ) : open ? null : (
        // No empty box. A tool with nothing in it is its own trigger, and a
        // large "还没有复合任务。" panel gave an unused control more of the
        // page than the work above it.
        <p className="text-[0.8rem] text-ink-3">
          会上说好「各自出一份 → 一个人汇总 → 大家投票 → 那个人定稿」的事，在这里建。
        </p>
      )}

      {open ? (
        <div className="mt-3 grid gap-3 rounded-lg border border-rule bg-raise p-3">
          <div className="flex flex-wrap gap-2">
            {(["VOTE", "SUBMIT"] as const).map((option) => (
              <button
                key={option}
                onClick={() => setKind(option)}
                aria-pressed={kind === option}
                className={
                  "rounded px-2.5 py-1 text-[0.8rem] " +
                  (kind === option
                    ? "bg-ink text-paper"
                    : "border border-rule text-ink-2 hover:border-ink")
                }
              >
                {option === "VOTE"
                  ? "投票型：填 → 汇总 → 投票 → 定稿"
                  : "提交型：填 → 汇总"}
              </button>
            ))}
          </div>

          <label className="grid gap-1">
            <span className="font-mono text-[0.72rem] text-ink-3">标题</span>
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              className="rounded border border-rule bg-paper px-2 py-1.5 text-[0.85rem]"
            />
          </label>

          <label className="grid gap-1">
            <span className="font-mono text-[0.72rem] text-ink-3">
              说明（参与的人会看到）
            </span>
            <textarea
              value={body}
              rows={2}
              onChange={(event) => setBody(event.target.value)}
              className="rounded border border-rule bg-paper px-2 py-1.5 text-[0.85rem]"
            />
          </label>

          <div className="grid gap-1">
            <span className="font-mono text-[0.72rem] text-ink-3">
              参与的人（至少两位）
            </span>
            <div className="flex flex-wrap gap-1.5">
              {people.map((person) => {
                const on = members.includes(person.actor_id);
                return (
                  <button
                    key={person.actor_id}
                    aria-pressed={on}
                    onClick={() =>
                      setMembers(
                        on
                          ? members.filter((id) => id !== person.actor_id)
                          : [...members, person.actor_id],
                      )
                    }
                    className={
                      "rounded-full px-2.5 py-1 text-[0.78rem] " +
                      (on
                        ? "bg-ink text-paper"
                        : "border border-rule text-ink-2 hover:border-ink")
                    }
                  >
                    {person.display_name}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid gap-1">
            <span className="font-mono text-[0.72rem] text-ink-3">
              谁来汇总（必须是上面选中的人之一）
            </span>
            <div className="flex flex-wrap gap-1.5">
              {people
                .filter((person) => members.includes(person.actor_id))
                .map((person) => (
                  <button
                    key={person.actor_id}
                    aria-pressed={owner === person.actor_id}
                    onClick={() => setOwner(person.actor_id)}
                    className={
                      "rounded-full px-2.5 py-1 text-[0.78rem] " +
                      (owner === person.actor_id
                        ? "bg-ink text-paper"
                        : "border border-rule text-ink-2 hover:border-ink")
                    }
                  >
                    {person.display_name}
                  </button>
                ))}
              {members.length ? null : (
                <span className="text-[0.78rem] text-ink-3">先选参与的人</span>
              )}
            </div>
          </div>

          {kind === "VOTE" ? (
            <label className="flex items-center gap-2">
              <span className="font-mono text-[0.72rem] text-ink-3">
                最后保留几条
              </span>
              <input
                type="number"
                min={1}
                max={30}
                value={keep}
                onChange={(event) => setKeep(Number(event.target.value))}
                className="w-16 rounded border border-rule bg-paper px-2 py-1 text-[0.85rem] tabular-nums"
              />
            </label>
          ) : null}

          <label className="grid gap-1">
            <span className="font-mono text-[0.72rem] text-ink-3">
              会上是怎么说的（原话，参与的人会看到）
            </span>
            <input
              value={span}
              onChange={(event) => setSpan(event.target.value)}
              placeholder="00:12:04 我们各人先出七八个，子恒汇总"
              className="rounded border border-rule bg-paper px-2 py-1.5 text-[0.85rem]"
            />
          </label>

          <div className="flex gap-2">
            <button
              disabled={!ready}
              onClick={send}
              className="rounded bg-ink px-3 py-1.5 text-[0.82rem] text-paper disabled:opacity-40"
            >
              建立复合任务
            </button>
            <button
              onClick={() => setOpen(false)}
              className="rounded border border-rule px-3 py-1.5 text-[0.82rem] text-ink-2"
            >
              取消
            </button>
          </div>
        </div>
      ) : null}
    </Zone>
  );
}

/** Where a compound task has got to, as a track rather than a word.
 *
 *  A task has one status and a compound task has an order of play; showing
 *  only the current stage name ("MERGING") tells a reader neither what came
 *  before it nor how much is left. The track is also what makes these rows
 *  read as a different kind of thing from the task cards above them without
 *  needing a different colour to say so.
 */
function StageTrack({ kind, stage }: { kind: string; stage: string }) {
  const order: string[] =
    kind === "VOTE"
      ? ["COLLECTING", "MERGING", "VOTING", "FINALIZING"]
      : ["COLLECTING", "MERGING"];
  const names: Record<string, string> = {
    COLLECTING: "填写",
    MERGING: "汇总",
    VOTING: "投票",
    FINALIZING: "定稿",
  };
  const done = stage === "DONE";
  const at = order.indexOf(stage);

  if (stage === "REVOKED") {
    return (
      <p className="mt-1.5 font-mono text-[0.72rem] text-ink-3">已撤销</p>
    );
  }
  return (
    <ol className="mt-1.5 flex flex-wrap items-center gap-1 font-mono text-[0.72rem]">
      {order.map((step, index) => {
        const passed = done || index < at;
        const here = !done && index === at;
        return (
          <li key={step} className="flex items-center gap-1">
            <span
              className={
                here
                  ? "rounded bg-accent-wash px-1.5 py-px text-accent"
                  : passed
                    ? "px-1.5 py-px text-ink-3 line-through decoration-rule"
                    : "px-1.5 py-px text-ink-3"
              }
            >
              {names[step] ?? step}
            </span>
            {index < order.length - 1 ? (
              <span className="text-rule-2">›</span>
            ) : null}
          </li>
        );
      })}
      {done ? <li className="ml-1 text-ok">已完成</li> : null}
    </ol>
  );
}
