import { useState } from "react";
import { messageId, postJson } from "../api";
import type { Task } from "../manage-types";
import { Button, Chip } from "../manage/TaskCard";
import type { Act } from "./MyTaskCard";

interface Option {
  option_id: string;
  text: string;
}

interface Contribution {
  contribution_id: string;
  actor_id: string;
  display_name?: string;
  contribution_type: "BALLOT" | "VOTE";
  status: string;
  payload?: {
    options?: Option[];
    scores?: Record<string, number>;
  } | null;
}

/** Question collection, ballot, vote.
 *
 *  Ported from the server-rendered page without changing what it does: the
 *  scoring rules, who may draft the ballot and when the final unlocks are all
 *  decided by the domain, and this is the same three steps in the same order.
 *  A meeting mid-vote would otherwise have lost the only surface that can
 *  finish it. */
export function VotePanel({
  task,
  me,
  act,
}: {
  task: Task;
  me: string;
  act: Act;
}) {
  const progress = task.collaboration_progress;
  const contributions = (progress?.contributions || []) as Contribution[];
  const ballot = contributions.find(
    (input) => input.contribution_type === "BALLOT",
  );
  const options = ballot?.payload?.options || [];
  const myVote = contributions.find(
    (input) => input.contribution_type === "VOTE" && input.actor_id === me,
  );
  const votes = contributions.filter(
    (input) => input.contribution_type === "VOTE",
  );
  const cast = votes.filter((vote) => vote.status !== "PENDING");
  const ready = Boolean(progress?.dependencies_ready);
  const myBallot =
    ballot?.actor_id === me && ballot.status === "PENDING" ? ballot : null;

  const waiting = (progress?.dependencies || []) as {
    upstream_title: string;
    satisfied: boolean;
  }[];
  const missing = waiting.filter((item) => !item.satisfied);

  return (
    <article className="rounded-md border border-rule-2 bg-raise px-3.5 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-[0.9rem]">{task.title}</b>
        <Chip tone={ready ? "live" : "plain"}>
          {ready ? "问题已收齐" : `等 ${missing.length} 项上游`}
        </Chip>
        <Chip>
          {cast.length}/{votes.length} 人已打分
        </Chip>
      </div>

      {!ready ? (
        <ul className="mt-2 grid gap-0.5 text-[0.79rem] text-ink-2">
          {missing.map((item) => (
            <li key={item.upstream_title}>· 等「{item.upstream_title}」验收</li>
          ))}
        </ul>
      ) : null}

      {ready && myBallot ? (
        <BallotDraft task={task} act={act} />
      ) : null}

      {options.length && !progress?.ballot_open ? (
        <OpenBallot task={task} options={options} act={act} />
      ) : null}

      {options.length ? (
        <Scoring
          task={task}
          options={options}
          mine={myVote}
          selectionCount={progress?.selection_count ?? options.length}
          act={act}
        />
      ) : null}
    </article>
  );
}

function BallotDraft({ task, act }: { task: Task; act: Act }) {
  return (
    <div className="mt-3 rounded border border-accent bg-accent-wash px-3 py-2.5">
      <p className="text-[0.82rem]">
        上游问题清单已全部验收，可以整理候选并开启投票。
      </p>
      <div className="mt-2">
        <Button
          onClick={() =>
            void act(
              () =>
                postJson(
                  `/api/action-items/${task.action_item_id}/ballot-draft`,
                  { message_id: messageId("ballot") },
                ),
              "候选已整理，检查后开启投票",
            )
          }
        >
          整理候选
        </Button>
      </div>
    </div>
  );
}

/** Publishing the shortlist.
 *
 *  Drafting it and voting on it were both reachable and the step between them
 *  was not, so a shortlist could be prepared and then never put to anybody.
 *  It is a separate step on purpose: the person who merged the questions gets
 *  to read them once more before everybody is asked to score them. */
function OpenBallot({
  task,
  options,
  act,
}: {
  task: Task;
  options: Option[];
  act: Act;
}) {
  return (
    <div className="mt-3 rounded border border-accent bg-accent-wash px-3 py-2.5">
      <p className="text-[0.82rem]">
        候选已整理好 {options.length} 条，检查无误就可以让大家打分。
      </p>
      <div className="mt-2">
        <Button
          onClick={() =>
            void act(
              () =>
                postJson(`/api/action-items/${task.action_item_id}/ballot`, {
                  options,
                  message_id: messageId("open-ballot"),
                }),
              "投票已开启，指定的人会看到打分界面",
            )
          }
        >
          开启投票
        </Button>
      </div>
    </div>
  );
}

function Scoring({
  task,
  options,
  mine,
  selectionCount,
  act,
}: {
  task: Task;
  options: Option[];
  mine?: Contribution;
  selectionCount: number;
  act: Act;
}) {
  const done = mine && mine.status !== "PENDING";
  const [scores, setScores] = useState<Record<string, number>>(
    () => mine?.payload?.scores || {},
  );

  return (
    <div className="mt-3 border-t border-rule-2 pt-3">
      <p className="mb-2 text-[0.79rem] text-ink-3">
        {done
          ? "你已经打过分了，下面是你给的分数。"
          : `给每条打 1–5 分，最后取前 ${selectionCount} 条。`}
      </p>
      <ol className="grid gap-1.5">
        {options.map((option, index) => (
          <li
            key={option.option_id}
            className="grid grid-cols-[1.4rem_1fr_auto] items-center gap-2 text-[0.82rem]"
          >
            <span className="tabular font-mono text-[0.7rem] text-ink-3">
              {index + 1}
            </span>
            <span>{option.text}</span>
            <span className="flex gap-1">
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  key={value}
                  disabled={done}
                  onClick={() =>
                    setScores((current) => ({
                      ...current,
                      [option.option_id]: value,
                    }))
                  }
                  aria-label={`${index + 1} 号 ${value} 分`}
                  className={`size-6 rounded font-mono text-[0.7rem] focus-visible:outline-2 focus-visible:outline-accent ${
                    scores[option.option_id] === value
                      ? "bg-accent text-white"
                      : "bg-sunk text-ink-3 hover:bg-rule disabled:hover:bg-sunk"
                  } disabled:cursor-default`}
                >
                  {value}
                </button>
              ))}
            </span>
          </li>
        ))}
      </ol>
      {!done ? (
        <div className="mt-3">
          <Button
            disabled={Object.keys(scores).length < options.length}
            onClick={() =>
              void act(
                () =>
                  postJson(`/api/action-items/${task.action_item_id}/vote`, {
                    scores,
                    message_id: messageId("vote"),
                  }),
                "打分已提交",
              )
            }
          >
            提交打分
          </Button>
          {Object.keys(scores).length < options.length ? (
            <span className="ml-2 text-[0.76rem] text-ink-3">
              还有 {options.length - Object.keys(scores).length} 条没打分
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
