import { useState } from "react";
import { messageId, postJson } from "../api";
import { Button } from "../manage/TaskCard";
import type { Act } from "./MyTaskCard";
import type { LexiconTopic, MemoryRow } from "./Handbook";

/** "草稿模式：有一个能讨论的版本…" → ["草稿模式", "有一个能讨论的版本…"].
 *
 *  The name is the part somebody repeats to a colleague, so it is set apart
 *  rather than left buried at the head of a sentence. The lexicon guarantees
 *  the separator; a value without one still renders, as its whole self. */
export function splitMode(label: string): [string, string] {
  const at = label.indexOf("：");
  return at < 0
    ? [label, ""]
    : [label.slice(0, at), label.slice(at + 1)];
}

/** Which questions this person has not answered yet. */
export function unanswered(
  topics: LexiconTopic[],
  memories: MemoryRow[],
  me: string,
): LexiconTopic[] {
  const answered = new Set(
    memories
      .filter(
        (memory) =>
          memory.actor_id === me &&
          memory.status !== "REJECTED" &&
          memory.value?.code,
      )
      .map((memory) => memory.topic),
  );
  return topics.filter((topic) => !answered.has(topic.topic));
}

/** One pass through the questions, in the bell.
 *
 *  Asking is a one-off: a person answers these once and then wants them out of
 *  the way. A standing panel on the page kept asking forever, which is why it
 *  read as clutter -- so the asking lives with the other things waiting on
 *  this person, and the answers live below the work as a record.
 *
 *  Answers are saved one at a time, as each is chosen. A single submit at the
 *  end would throw away four answers because somebody closed the popover on
 *  the fifth. */
export function Survey({
  topics,
  act,
  onDone,
}: {
  topics: LexiconTopic[];
  act: Act;
  onDone: () => void;
}) {
  const [started, setStarted] = useState(false);
  const [at, setAt] = useState(0);
  const topic = topics[at];
  const [picked, setPicked] = useState("");

  // An invitation before the first question. Dropping somebody straight into
  // "关于交付节奏，你更接近哪一种？" with no framing reads as an interrogation
  // in the middle of their task list -- they need to know what this is for and
  // that it is theirs to decline before the first answer is asked of them.
  if (!started)
    return (
      <article className="mb-2 rounded border border-accent bg-accent-wash px-3 py-3">
        <b className="text-[0.86rem]">个人协作习惯</b>
        <p className="mt-1 text-[0.8rem] text-ink-2">
          通过问卷填写一下你的个人协作偏好吧。
        </p>
        <p className="mt-1 text-[0.75rem] text-ink-3">
          共 {topics.length} 题，答完的会显示给同事，用来告诉他们怎么跟你配合。
        </p>
        <div className="mt-2.5 flex gap-2">
          <Button onClick={() => setStarted(true)}>开始答题</Button>
          <Button tone="ghost" onClick={onDone}>
            以后再说
          </Button>
        </div>
      </article>
    );

  if (!topic) return null;

  const save = (code: string) => {
    setPicked(code);
    void act(async () => {
      await postJson("/api/memories/declare", {
        topic: topic.topic,
        code,
        message_id: messageId("declare"),
      });
      setPicked("");
      if (at + 1 >= topics.length) onDone();
      else setAt(at + 1);
    }, "已记下");
  };

  return (
    <article className="mb-2 rounded border border-accent bg-accent-wash px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <b className="text-[0.84rem]">{topic.title}</b>
        <span className="shrink-0 font-mono text-[0.66rem] text-ink-3">
          {at + 1}/{topics.length}
        </span>
      </div>
      <p className="mt-0.5 mb-2 text-[0.79rem] text-ink-2">{topic.prompt}</p>
      <div className="grid gap-1">
        {topic.values.map((value) => {
          const [mode, said] = splitMode(value.label);
          return (
            <button
              key={value.code}
              disabled={Boolean(picked)}
              onClick={() => save(value.code)}
              className="rounded border border-rule bg-raise px-2.5 py-1.5 text-left text-[0.81rem] hover:border-accent focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
            >
              <b className="font-semibold">{mode}</b>
              {said ? <span className="text-ink-2">　{said}</span> : null}
              {value.collaborator_hint ? (
                <span className="mt-0.5 block text-[0.74rem] text-ink-3">
                  同事会看到：{value.collaborator_hint}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      <div className="mt-2 flex justify-between">
        <Button tone="ghost" onClick={onDone}>
          以后再说
        </Button>
        {at + 1 < topics.length ? (
          <Button tone="ghost" onClick={() => setAt(at + 1)}>
            跳过这题
          </Button>
        ) : null}
      </div>
    </article>
  );
}
