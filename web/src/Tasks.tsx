import { useCallback, useEffect, useRef, useState } from "react";
import { getJson } from "./api";
import type { ManageState, Task } from "./manage-types";
import { formatDay } from "./manage/schedule";
import { TaskCard } from "./manage/TaskCard";
import { Blank, Zone } from "./manage/Zone";
import { Bell } from "./tasks/Bell";
import { Handbook } from "./tasks/Handbook";
import { unanswered } from "./tasks/Survey";
import { IdentityGate, WhoAmI, signOut, storedActor } from "./tasks/WhoAmI";
import { MyTaskCard, type Act } from "./tasks/MyTaskCard";
import { VotePanel } from "./tasks/VotePanel";
import { CompoundPanel } from "./tasks/CompoundPanel";

import { tokenKey } from "./base";

const DONE = new Set(["ACCEPTED", "AGGREGATED", "ARCHIVED"]);

export default function TasksPage() {
  const [ready, setReady] = useState(() =>
    Boolean(localStorage.getItem(tokenKey())),
  );
  const [state, setState] = useState<ManageState | null>(null);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const [askSurvey, setAskSurvey] = useState(false);
  const cards = useRef(new Map<string, HTMLElement>());

  const load = useCallback(async () => {
    try {
      setState(await getJson<ManageState>("/api/state?surface=tasks"));
      setError("");
    } catch (problem) {
      setError((problem as Error).message);
    }
  }, []);

  useEffect(() => {
    if (ready) void load();
  }, [ready, load]);

  const act: Act = useCallback(
    async (run, done) => {
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

  const focus = useCallback((id: string) => {
    setSelected(id);
    cards.current
      .get(id)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  const bind = useCallback(
    (id: string) => (node: HTMLElement | null) => {
      if (node) cards.current.set(id, node);
      else cards.current.delete(id);
    },
    [],
  );

  if (!ready) return <IdentityGate onReady={() => setReady(true)} />;

  if (error)
    return (
      <main className="mx-auto max-w-lg px-4 py-16">
        <h1 className="text-xl font-bold">读不到你的任务</h1>
        <p className="mt-3 text-ink-2">{error}</p>
        <button
          onClick={() => {
            signOut();
            setReady(false);
            setError("");
          }}
          className="mt-4 rounded border border-rule px-3 py-1.5 text-[0.85rem] hover:bg-sunk"
        >
          换个身份
        </button>
      </main>
    );

  if (!state)
    return <main className="mx-auto max-w-lg p-8 text-ink-3">正在读取…</main>;

  const me = state.principal.actor_id;
  const mine = state.tasks.filter(
    (task) => task.is_mine || task.is_collaborator || task.my_assignment,
  );
  const live = mine.filter((task) => !DONE.has(task.status));
  const done = mine.filter((task) => DONE.has(task.status));
  const votes = state.tasks.filter(
    (task) => task.collaboration_progress && involvesMe(task, me),
  );
  // Finished ones stay out of the zone but keep their place in history: a
  // shape that ran to a decision is a thing people go back and look at.
  const compound = (state.compound_tasks ?? []).filter(
    (task) => task.stage !== "REVOKED",
  );
  const compoundOwed = compound.filter((task) => task.my_turn).length;
  const coordinator = state.allowed_surfaces.includes("manage");

  return (
    <div className="mx-auto max-w-[62rem] px-4 pb-20 sm:px-8">
      <header className="mb-6 flex flex-wrap items-center gap-x-5 gap-y-3 border-b border-rule py-6">
        <h1 className="text-xl font-bold tracking-tight">我的任务</h1>
        <span className="tabular font-mono text-[0.76rem] text-ink-2">
          {storedActor()} · {live.length} 项进行中 · 现在{" "}
          {formatDay(state.episode.current_sim_time as string)}
        </span>
        <div className="ml-auto flex items-center gap-3">
          <Bell
            state={state}
            act={act}
            onFocus={focus}
            openSurvey={askSurvey}
            onSurveyOpened={() => setAskSurvey(false)}
          />
          {coordinator ? (
            <a
              className="font-mono text-[0.75rem] text-accent underline"
              href="/manage"
            >
              会议工作台
            </a>
          ) : null}
          <WhoAmI
            name={storedActor()}
            coordinator={coordinator}
            onSwitched={() => {
              // The new identity sees a different meeting: drop what the last
              // one had open rather than carrying a selection across people.
              setSelected(null);
              setHistory(false);
              setState(null);
              void load();
            }}
          />
        </div>
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

      <Zone
        n="01"
        name="进行中"
        pending={live.length}
        pendingLabel={
          history ? `已验收 ${done.length} 项` : `${live.length} 项进行中`
        }
        why="派发要你回应、进展要你报、做完了在这里提交。每张卡片只留跟当前状态有关的动作。"
      >
        {history ? (
          done.length ? (
            <div className="grid gap-2">
              {done.map((task) => (
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
            <Blank>还没有验收通过的任务。</Blank>
          )
        ) : live.length ? (
          <div className="grid gap-2">
            {live.map((task) => (
              <MyTaskCard
                key={task.action_item_id}
                task={task}
                state={state}
                act={act}
                selected={selected === task.action_item_id}
                cardRef={bind(task.action_item_id)}
                onSelect={() => setSelected(task.action_item_id)}
              />
            ))}
          </div>
        ) : (
          <Blank>没有派给你的任务。</Blank>
        )}

        <div className="mt-3 text-right">
          <button
            onClick={() => setHistory((open) => !open)}
            className="font-mono text-[0.75rem] text-ink-3 underline hover:text-ink"
          >
            {history ? "← 返回进行中" : `🕐 已验收 ${done.length} 项`}
          </button>
        </div>
      </Zone>

      {compound.length ? (
        <Zone
          n="02"
          name="复合任务"
          anchor="zone-compound"
          pending={compoundOwed}
          pendingLabel={
            compoundOwed ? `${compoundOwed} 项轮到你` : "都不在你这儿"
          }
          why="会上定下的多环节协作：大家各自填 → 一个人汇总 → 大家投票 → 那个人定稿。每一环节都要所有人交齐才往下走。"
        >
          <div className="grid gap-2">
            {compound.map((task) => (
              <CompoundPanel
                key={task.compound_task_id}
                task={task}
                me={me}
                reload={load}
              />
            ))}
          </div>
        </Zone>
      ) : null}

      {votes.length ? (
        <Zone
          n="03"
          name="投票"
          pending={votes.length}
          pendingLabel={`${votes.length} 项需要你参与`}
          why="问题清单收齐后由整理人生成候选，指定的投票人全部打完分才解锁定稿。"
        >
          <div className="grid gap-2">
            {votes.map((task) => (
              <VotePanel
                key={task.action_item_id}
                task={task}
                me={me}
                act={act}
              />
            ))}
          </div>
        </Zone>
      ) : null}

      <Handbook
        memories={state.memories ?? []}
        me={me}
        act={act}
        onOpenSurvey={() => setAskSurvey(true)}
        unansweredCount={
          unanswered(
            state.memory_lexicon?.topics ?? [],
            state.memories ?? [],
            me,
          ).length
        }
      />
    </div>
  );
}

function involvesMe(task: Task, me: string): boolean {
  const inputs = task.collaboration_progress?.contributions || [];
  return (
    task.owner_actor_id === me ||
    Boolean(task.is_collaborator) ||
    inputs.some((input) => input.actor_id === me)
  );
}
