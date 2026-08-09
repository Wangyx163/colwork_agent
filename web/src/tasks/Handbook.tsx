import { useState } from "react";
import { messageId, postJson } from "../api";
import { Button, Chip } from "../manage/TaskCard";
import { Zone } from "../manage/Zone";
import type { Act } from "./MyTaskCard";

export interface LexiconValue {
  code: string;
  label: string;
  collaborator_hint?: string;
}

export interface LexiconTopic {
  topic: string;
  title: string;
  prompt: string;
  origin?: string;
  values: LexiconValue[];
}

export interface MemoryRow {
  memory_id: string;
  actor_id: string;
  topic: string;
  status: string;
  value?: {
    code?: string;
    statement?: string;
    collaborator_hint?: string;
  } | null;
}

/** How you want to be worked with.
 *
 *  Two different things live here and they are not the same act. The system
 *  can *observe* a habit and offer it back for confirmation, and a person can
 *  *declare* one outright. Only the second lets somebody say something about
 *  themselves the system has not seen yet, which is the whole point of having
 *  it -- an earlier pass shipped only the confirmations and quietly took that
 *  away.
 *
 *  Nothing here changes permissions, task state or acceptance. It is advice
 *  to colleagues, which is why it sits below the work rather than among it. */
export function Handbook({
  topics,
  memories,
  me,
  act,
}: {
  topics: LexiconTopic[];
  memories: MemoryRow[];
  me: string;
  act: Act;
}) {
  const mine = memories.filter((memory) => memory.actor_id === me);
  const drafts = mine.filter((memory) => memory.status === "PRIVATE_DRAFT");
  if (!topics.length && !mine.length) return null;

  return (
    <Zone
      n="03"
      name="我的协作说明书"
      pending={drafts.length}
      pendingLabel={
        drafts.length ? `${drafts.length} 条待你确认` : `${mine.length} 条`
      }
      why="告诉同事该怎么跟你配合。不影响权限、任务状态或验收——纯粹是给人看的。"
    >
      {drafts.length ? (
        <div className="mb-4 grid gap-2">
          <p className="text-[0.82rem] font-semibold">系统从你的协作里观察到的</p>
          {drafts.map((memory) => (
            <div
              key={memory.memory_id}
              className="rounded-md border border-rule-2 px-3.5 py-3"
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <b className="text-[0.85rem]">
                  {memory.value?.statement || memory.topic}
                </b>
                <Chip tone="warn">待你确认</Chip>
              </div>
              <div className="mt-2 flex gap-2">
                <Button
                  onClick={() =>
                    void act(
                      () =>
                        postJson(
                          `/api/memories/${memory.memory_id}/confirm`,
                          { message_id: messageId("memory") },
                        ),
                      "已确认，同事会看到这一条",
                    )
                  }
                >
                  确实是这样
                </Button>
                <Button
                  tone="ghost"
                  onClick={() =>
                    void act(
                      () =>
                        postJson(
                          `/api/memories/${memory.memory_id}/reject`,
                          { message_id: messageId("memory") },
                        ),
                      "已拒绝，不会再提示",
                    )
                  }
                >
                  不是这样
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {topics.length ? (
        <div className="grid gap-2">
          <p className="text-[0.82rem] font-semibold">你自己想说的</p>
          {topics.map((topic) => (
            <TopicRow
              key={topic.topic}
              topic={topic}
              current={
                mine.find(
                  (memory) =>
                    memory.topic === topic.topic &&
                    memory.status !== "REJECTED" &&
                    memory.value?.code,
                )?.value?.code || ""
              }
              act={act}
            />
          ))}
        </div>
      ) : null}
    </Zone>
  );
}

function TopicRow({
  topic,
  current,
  act,
}: {
  topic: LexiconTopic;
  current: string;
  act: Act;
}) {
  const [picked, setPicked] = useState(current);
  const chosen = topic.values.find((value) => value.code === picked);

  return (
    <div className="rounded-md border border-rule-2 px-3.5 py-3">
      <p className="text-[0.85rem] font-semibold">{topic.title}</p>
      <p className="mt-0.5 mb-2 text-[0.79rem] text-ink-3">{topic.prompt}</p>
      <div className="grid gap-1">
        {topic.values.map((value) => (
          <label
            key={value.code}
            className="flex items-start gap-2 text-[0.82rem]"
          >
            <input
              type="radio"
              name={topic.topic}
              className="mt-1"
              checked={picked === value.code}
              onChange={() => setPicked(value.code)}
            />
            {value.label}
          </label>
        ))}
      </div>
      {chosen?.collaborator_hint ? (
        <p className="mt-2 rounded-r border-l-[3px] border-accent bg-accent-wash px-3 py-1.5 text-[0.78rem]">
          同事会看到：{chosen.collaborator_hint}
        </p>
      ) : null}
      {picked && picked !== current ? (
        <div className="mt-2">
          <Button
            onClick={() =>
              void act(
                () =>
                  postJson("/api/memories/declare", {
                    topic: topic.topic,
                    code: picked,
                    message_id: messageId("declare"),
                  }),
                "已更新你的协作说明书",
              )
            }
          >
            保存
          </Button>
        </div>
      ) : null}
    </div>
  );
}
