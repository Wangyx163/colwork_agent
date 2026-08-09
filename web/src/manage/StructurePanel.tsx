import { useState } from "react";
import { messageId, postJson } from "../api";
import type { ManageState, Task } from "../manage-types";
import { Button, Chip } from "./TaskCard";
import { Blank, Zone } from "./Zone";

type Act = (run: () => Promise<unknown>, done: string) => Promise<void>;

/** Declaring the one compound shape the meeting supports.
 *
 *  The flow it sets up is: several people each collect questions, one person
 *  merges them into a shortlist, everybody scores it, and the final owner
 *  writes the result. None of that is a workflow engine -- the collection
 *  tasks stay ordinary tasks with ordinary versions and acceptance -- so all
 *  this declares is which task waits on which, who scores, and how many
 *  survive.
 *
 *  It has to be declared before dispatch: it binds the merge task to the
 *  *accepted* version of each upstream task, and a task already in flight has
 *  no accepted version to bind to.
 */
export function StructurePanel({
  state,
  act,
}: {
  state: ManageState;
  act: Act;
}) {
  const [open, setOpen] = useState(false);
  const [decision, setDecision] = useState("");
  const [upstream, setUpstream] = useState<string[]>([]);
  const [owner, setOwner] = useState("");
  const [voters, setVoters] = useState<string[]>([]);
  const [keep, setKeep] = useState(8);

  const declared = state.tasks.filter((task) => task.collaboration_progress);
  const eligible = state.tasks.filter(
    (task) =>
      task.status === "PENDING_CONFIRMATION" &&
      !task.owner_actor_id &&
      !task.collaboration_progress,
  );

  const send = () => {
    const target = state.tasks.find(
      (task) => task.action_item_id === decision,
    );
    void act(async () => {
      await postJson("/api/collaboration-structures/question-vote", {
        collection_action_item_ids: upstream,
        decision_action_item_id: decision,
        final_owner_actor_id: owner,
        voter_actor_ids: voters,
        selection_count: keep,
        // The domain requires a meeting reference: a structure is something
        // the meeting decided, not something the console invented.
        source_span: target?.source_span || "",
        message_id: messageId("structure"),
      });
      setOpen(false);
      setDecision("");
      setUpstream([]);
      setVoters([]);
    }, "复合结构已声明，按普通任务派发即可");
  };

  const ready =
    decision && upstream.length > 0 && owner && voters.length > 0;

  return (
    <Zone
      n="02b"
      name="收集 → 投票 → 定稿"
      pending={declared.length}
      pendingLabel={
        declared.length ? `${declared.length} 组进行中` : "还没有声明"
      }
      ownerOnly
      why="会议上说好「几个人分头收集 → 一个人汇总 → 大家打分 → 定稿」时，在这里说明一次。底层还是普通任务：照常派发、执行、验收，只是汇总任务会等上游全部验收完才解锁。"
    >
      {declared.length ? (
        <div className="mb-3 grid gap-2">
          {declared.map((task) => (
            <Declared key={task.action_item_id} task={task} act={act} />
          ))}
        </div>
      ) : null}

      {eligible.length < 2 ? (
        <Blank>
          需要至少两个还没派发的任务才能组一组（一个收集、一个汇总）。
        </Blank>
      ) : !open ? (
        <Button onClick={() => setOpen(true)}>声明一组</Button>
      ) : (
        <div className="grid gap-3 rounded-md border border-rule-2 bg-ground px-3.5 py-3">
          <Field label="汇总定稿的那个任务">
            <select
              value={decision}
              onChange={(event) => {
                setDecision(event.target.value);
                setUpstream((current) =>
                  current.filter((id) => id !== event.target.value),
                );
              }}
              className={INPUT}
            >
              <option value="">请选择</option>
              {eligible.map((task) => (
                <option key={task.action_item_id} value={task.action_item_id}>
                  {task.title}
                </option>
              ))}
            </select>
          </Field>

          <Fieldset label="上游收集任务（可多选）">
            {eligible
              .filter((task) => task.action_item_id !== decision)
              .map((task) => (
                <Check
                  key={task.action_item_id}
                  checked={upstream.includes(task.action_item_id)}
                  onChange={(on) =>
                    setUpstream((current) =>
                      on
                        ? [...current, task.action_item_id]
                        : current.filter((id) => id !== task.action_item_id),
                    )
                  }
                >
                  {task.title}
                </Check>
              ))}
          </Fieldset>

          <Field label="谁来定稿">
            <select
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              className={INPUT}
            >
              <option value="">请选择</option>
              {state.participants.map((person) => (
                <option key={person.actor_id} value={person.actor_id}>
                  {person.display_name}
                </option>
              ))}
            </select>
          </Field>

          <Fieldset label="谁来打分（全部打完才解锁定稿）">
            {state.participants.map((person) => (
              <Check
                key={person.actor_id}
                checked={voters.includes(person.actor_id)}
                onChange={(on) =>
                  setVoters((current) =>
                    on
                      ? [...current, person.actor_id]
                      : current.filter((id) => id !== person.actor_id),
                  )
                }
              >
                {person.display_name}
              </Check>
            ))}
          </Fieldset>

          <Field label="最终保留几条">
            <input
              type="number"
              min={1}
              max={8}
              value={keep}
              onChange={(event) => setKeep(Number(event.target.value))}
              className={`${INPUT} w-24`}
            />
          </Field>

          <div className="flex gap-2">
            <Button disabled={!ready} onClick={send}>
              声明
            </Button>
            <Button tone="ghost" onClick={() => setOpen(false)}>
              取消
            </Button>
          </div>
          {!ready ? (
            <p className="text-[0.75rem] text-ink-3">
              汇总任务、至少一个上游、定稿人、至少一个投票人，都要选。
            </p>
          ) : null}
        </div>
      )}
    </Zone>
  );
}

function Declared({ task, act }: { task: Task; act: Act }) {
  const [revoking, setRevoking] = useState(false);
  const [reason, setReason] = useState("");
  const progress = task.collaboration_progress;
  const dependencies = (progress?.dependencies || []) as {
    upstream_title: string;
    satisfied: boolean;
  }[];
  const satisfied = dependencies.filter((item) => item.satisfied).length;

  return (
    <article className="rounded-md border border-rule-2 px-3.5 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-[0.88rem]">{task.title}</b>
        <Chip tone={progress?.dependencies_ready ? "live" : "plain"}>
          上游 {satisfied}/{dependencies.length} 已验收
        </Chip>
        <Chip>保留 {progress?.selection_count ?? "—"} 条</Chip>
      </div>
      <ul className="mt-1.5 grid gap-0.5 text-[0.79rem] text-ink-2">
        {dependencies.map((item) => (
          <li key={item.upstream_title}>
            {item.satisfied ? "✓" : "·"} {item.upstream_title}
          </li>
        ))}
      </ul>

      {/* A structure declared over the wrong tasks blocks the merge task
          forever: it waits on upstreams that will never be accepted, and no
          amount of ordinary work clears it. Undoing has to be reachable. */}
      <div className="mt-2">
        <button
          onClick={() => setRevoking((open) => !open)}
          className="font-mono text-[0.73rem] text-ink-3 underline hover:text-ink"
        >
          {revoking ? "取消" : "撤销这组"}
        </button>
      </div>
      {revoking ? (
        <div className="mt-2 grid gap-2 rounded border border-warn bg-warn-wash px-3 py-2.5">
          <label className="grid gap-1 text-[0.79rem]">
            撤销原因
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="rounded border border-rule bg-raise px-2 py-1 text-[0.82rem]"
            />
          </label>
          <p className="text-[0.77rem] text-ink-2">
            依赖和已记录的参与会被移除，底层任务不受影响。
          </p>
          <div>
            <Button
              disabled={!reason.trim()}
              onClick={() =>
                void act(async () => {
                  const result = await postJson<{
                    removed_dependency_count?: number;
                    removed_participation_input_count?: number;
                  }>(
                    `/api/collaboration-structures/question-vote/${task.action_item_id}/revoke`,
                    { reason, message_id: messageId("revoke") },
                  );
                  void result;
                  setRevoking(false);
                }, "结构已撤销")
              }
            >
              确认撤销
            </Button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

const INPUT =
  "rounded border border-rule bg-raise px-2 py-1 text-[0.82rem] focus-visible:outline-2 focus-visible:outline-accent";

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="grid gap-1 text-[0.79rem]">
      {label}
      {children}
    </label>
  );
}

function Fieldset({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="grid gap-1 text-[0.79rem]">
      <legend className="mb-1">{label}</legend>
      <div className="flex flex-wrap gap-x-3 gap-y-1">{children}</div>
    </fieldset>
  );
}

function Check({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: (on: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="inline-flex items-center gap-1.5 text-[0.81rem]">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      {children}
    </label>
  );
}
