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
  return at < 0 ? [label, ""] : [label.slice(0, at), label.slice(at + 1)];
}

/** The questions this person can answer about themselves, still unanswered.
 *
 *  Only self-declared topics: the observed ones are not asked, they are
 *  noticed and offered back for confirmation. Asking somebody to declare one
 *  would make the manual self-reported, and the server refuses it anyway. */
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
  return topics.filter(
    (topic) =>
      topic.origin !== "SYSTEM_OBSERVED" && !answered.has(topic.topic),
  );
}

type Stage = "intro" | "asking" | "review";

/** One pass through the questions.
 *
 *  Answers are held here and sent together at the end. Saving each one as it
 *  is picked made the fifth question unable to change the second -- and these
 *  questions inform each other, which is exactly when somebody wants to go
 *  back. The trade is that leaving halfway loses the pass, so the answers are
 *  kept in this browser until they are sent.
 */
export function Survey({
  topics,
  act,
  onDone,
}: {
  topics: LexiconTopic[];
  act: Act;
  onDone: () => void;
}) {
  const [stage, setStage] = useState<Stage>("intro");
  const [at, setAt] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string>>(() => {
    try {
      return JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
    } catch {
      return {};
    }
  });
  const [sending, setSending] = useState(false);

  const remember = (topic: string, code: string) => {
    const next = { ...answers, [topic]: code };
    setAnswers(next);
    localStorage.setItem(DRAFT_KEY, JSON.stringify(next));
  };

  const done = topics.filter((topic) => answers[topic.topic]).length;
  const complete = done === topics.length;

  if (stage === "intro")
    return (
      <Frame>
        <b className="text-[0.86rem]">个人协作习惯</b>
        <p className="mt-1 text-[0.8rem] text-ink-2">
          通过问卷填写一下你的个人协作偏好吧。
        </p>
        <p className="mt-1 text-[0.75rem] text-ink-3">
          共 {topics.length} 题，全部答完才会提交。答完的会显示给同事，用来告诉他们怎么跟你配合。
        </p>
        <div className="mt-2.5 flex gap-2">
          <Button onClick={() => setStage("asking")}>
            {done ? "继续答题" : "开始答题"}
          </Button>
          <Button tone="ghost" onClick={onDone}>
            以后再说
          </Button>
        </div>
      </Frame>
    );

  if (stage === "review") {
    const send = () => {
      setSending(true);
      void act(async () => {
        // One request per answer, in order. The server treats each as its own
        // idempotent declaration, so a failure halfway leaves the earlier ones
        // standing rather than rolling back somebody's answers.
        for (const topic of topics) {
          await postJson("/api/memories/declare", {
            topic: topic.topic,
            code: answers[topic.topic],
            message_id: messageId(`declare-${topic.topic}`),
          });
        }
        localStorage.removeItem(DRAFT_KEY);
        onDone();
      }, "协作说明书已更新，同事能看到了");
    };

    return (
      <Frame>
        <b className="text-[0.86rem]">确认一下</b>
        <ul className="mt-2 grid gap-1.5">
          {topics.map((topic, index) => {
            const chosen = topic.values.find(
              (value) => value.code === answers[topic.topic],
            );
            const [mode] = splitMode(chosen?.label ?? "");
            return (
              <li
                key={topic.topic}
                className="flex items-baseline justify-between gap-2 text-[0.81rem]"
              >
                <span className="text-ink-3">{topic.title}</span>
                <button
                  onClick={() => {
                    setAt(index);
                    setStage("asking");
                  }}
                  className="font-semibold text-accent underline focus-visible:outline-2 focus-visible:outline-accent"
                >
                  {mode || "未选"}
                </button>
              </li>
            );
          })}
        </ul>
        <div className="mt-3 flex gap-2">
          <Button disabled={!complete || sending} onClick={send}>
            提交
          </Button>
          <Button
            tone="ghost"
            onClick={() => {
              setAt(topics.length - 1);
              setStage("asking");
            }}
          >
            上一题
          </Button>
        </div>
      </Frame>
    );
  }

  const topic = topics[at];
  if (!topic) return null;
  const picked = answers[topic.topic];

  return (
    <Frame>
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
          const chosen = picked === value.code;
          return (
            <button
              key={value.code}
              onClick={() => remember(topic.topic, value.code)}
              aria-pressed={chosen}
              className={`rounded border px-2.5 py-1.5 text-left text-[0.81rem] focus-visible:outline-2 focus-visible:outline-accent ${
                chosen
                  ? "border-accent bg-accent-wash"
                  : "border-rule bg-raise hover:border-accent"
              }`}
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
      <div className="mt-2.5 flex items-center gap-2">
        <Button
          tone="ghost"
          disabled={at === 0}
          onClick={() => setAt(at - 1)}
        >
          上一题
        </Button>
        {at + 1 < topics.length ? (
          <Button disabled={!picked} onClick={() => setAt(at + 1)}>
            下一题
          </Button>
        ) : (
          <Button disabled={!complete} onClick={() => setStage("review")}>
            看一遍再提交
          </Button>
        )}
        <span className="ml-auto text-[0.74rem] text-ink-3">
          已答 {done}/{topics.length}
        </span>
      </div>
      <button
        onClick={onDone}
        className="mt-1.5 text-[0.75rem] text-ink-3 underline hover:text-ink"
      >
        以后再说（已选的会留着）
      </button>
    </Frame>
  );
}

const DRAFT_KEY = "collabSurveyDraft";

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <article className="mb-2 rounded border border-accent bg-accent-wash px-3 py-3">
      {children}
    </article>
  );
}
