import { messageId, postJson } from "../api";
import { Button, Chip } from "../manage/TaskCard";
import { Blank, Zone } from "../manage/Zone";
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

/** Memory: what this system believes about how someone works.
 *
 *  Three rules decide everything on this panel, and dropping any one of them
 *  turns it into something worth being uneasy about.
 *
 *  It never participates in a decision. Not permissions, not task state, not
 *  escalation level, not acceptance. It is advice to colleagues, which is why
 *  it sits below the work rather than among it.
 *
 *  Observed is not the same as true. Anything the system noticed arrives as a
 *  PRIVATE_DRAFT and is visible to nobody else until the person confirms it --
 *  otherwise the system would be labelling people behind their backs.
 *
 *  It states what a colleague should do, not what a person is like. That is
 *  why every entry carries a collaborator hint: not "he works from drafts",
 *  but "send him the rough version early, editing together beats waiting". */
export function Handbook({
  n,
  memories,
  me,
  act,
  onOpenSurvey,
  unansweredCount,
}: {
  /** Position in the page, decided by the page. */
  n: string;
  memories: MemoryRow[];
  me: string;
  act: Act;
  onOpenSurvey: () => void;
  unansweredCount: number;
}) {
  const mine = memories.filter((memory) => memory.actor_id === me);
  const drafts = mine.filter((memory) => memory.status === "PRIVATE_DRAFT");
  // CONFIRMED, not "anything that is not a draft". The looser test also swept
  // in SUPERSEDED and REJECTED rows -- eight of them against five real ones on
  // the live meeting -- so the list showed entries colleagues cannot see, each
  // with a 撤下 button the domain refuses because only a confirmed memory may
  // be withdrawn. That is the "cannot take it down" report: the button worked,
  // it was attached to rows that were already down.
  const confirmed = mine.filter((memory) => memory.status === "CONFIRMED");

  return (
    <Zone
      n={n}
      name="Memory"
      why="系统观察到的你的协作习惯。不影响权限、任务状态或验收——只用来告诉同事该怎么跟你配合，而且没经你确认的，别人看不到。"
      pending={drafts.length}
      pendingLabel={
        drafts.length ? `${drafts.length} 条待你确认` : `${confirmed.length} 条`
      }
    >
      {drafts.length ? (
        <div className="mb-3 grid gap-2">
          {drafts.map((memory) => (
            <div
              key={memory.memory_id}
              className="rounded-md border border-warn bg-warn-wash px-3.5 py-3"
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <b className="text-[0.85rem]">
                  {memory.value?.statement || memory.topic}
                </b>
                <Chip tone="warn">只有你看得到</Chip>
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

      {confirmed.length ? (
        <ul className="grid gap-1.5">
          {confirmed.map((memory) => (
            <li
              key={memory.memory_id}
              className="flex items-start gap-2 rounded border border-rule-2 px-3 py-2 text-[0.82rem]"
            >
              <span className="mt-[0.42rem] size-[0.45rem] shrink-0 rounded-full bg-ok" />
              <span className="min-w-0 flex-1">
                {memory.value?.collaborator_hint ||
                  memory.value?.statement ||
                  memory.topic}
              </span>
              {/* Anything shown to colleagues has to be removable by the
                  person it describes, or "you have the final say" is only a
                  slogan. Re-answering supersedes; this is for when the honest
                  answer is that it should not be up at all. */}
              <button
                onClick={() =>
                  void act(
                    () =>
                      postJson(
                        `/api/memories/${memory.memory_id}/withdraw`,
                        { message_id: messageId("memory") },
                      ),
                    "已撤下，同事看不到了；想重新填可以再答一次",
                  )
                }
                className="shrink-0 font-mono text-[0.72rem] text-ink-3 underline hover:text-bad focus-visible:outline-2 focus-visible:outline-accent"
              >
                撤下
              </button>
            </li>
          ))}
        </ul>
      ) : drafts.length ? null : (
        <Blank>还没有确认过的条目。</Blank>
      )}

      {unansweredCount ? (
        <p className="mt-3 text-[0.8rem] text-ink-3">
          还有 {unansweredCount} 个协作偏好没填，
          <button
            onClick={onOpenSurvey}
            className="text-accent underline focus-visible:outline-2 focus-visible:outline-accent"
          >
            去闹铃里填
          </button>
          。
        </p>
      ) : null}
    </Zone>
  );
}
