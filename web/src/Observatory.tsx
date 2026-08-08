import { useEffect, useState } from "react";
import { Tile } from "./components";
import {
  AuditPanel,
  ContextPanel,
  GatePanel,
  LineagePanel,
  OutboxPanel,
  ResultPanel,
  TokenPanel,
} from "./panels";
import type { Observatory } from "./types";
import { authHeaders } from "./api";

export default function ObservatoryPage() {
  const [data, setData] = useState<Observatory | null>(null);
  const [error, setError] = useState("");
  const [target, setTarget] = useState<{ run: string; episode: string } | null>(
    null,
  );
  const [railOpen, setRailOpen] = useState(true);

  useEffect(() => {
    const query = target
      ? `?run_id=${encodeURIComponent(target.run)}&episode_id=${encodeURIComponent(target.episode)}`
      : "";
    let cancelled = false;
    fetch(`/api/observatory${query}`, { headers: authHeaders() })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.message || "读取失败");
        return body as Observatory;
      })
      .then((body) => {
        if (!cancelled) {
          setData(body);
          setError("");
        }
      })
      .catch((problem: Error) => {
        if (!cancelled) setError(problem.message);
      });
    return () => {
      cancelled = true;
    };
  }, [target]);

  if (error) {
    return (
      <main className="mx-auto max-w-2xl p-8">
        <h1 className="text-xl font-bold">打不开 Observatory</h1>
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
  }

  if (!data) {
    return (
      <main className="mx-auto max-w-2xl p-8 text-ink-3">正在读取运行数据…</main>
    );
  }

  const h = data.headline;
  const rate = h.citation_hallucination_rate;

  return (
    <div className="mx-auto max-w-[78rem] px-4 pb-20 sm:px-8">
      <header className="mb-6 flex flex-wrap items-baseline gap-x-6 gap-y-3 border-b border-rule py-8">
        <h1 className="text-xl font-bold tracking-tight sm:text-2xl">
          Agent Observatory
        </h1>
        <span className="inline-flex items-center gap-2 rounded border border-rule bg-raise px-3 py-1 font-mono text-[0.78rem] text-ink-2">
          运行 <b className="font-semibold text-ink">{data.run.run_id}</b> ·{" "}
          {data.audit.total} 事件
        </span>
        <a
          href="/manage"
          className="ml-auto font-mono text-[0.75rem] text-accent underline"
        >
          回到工作台
        </a>
      </header>

      <div
        className="grid items-start gap-4 transition-[grid-template-columns] duration-150"
        style={{
          gridTemplateColumns: railOpen ? "11.5rem 1fr" : "2.6rem 1fr",
        }}
      >
        <aside className="sticky top-4 overflow-hidden rounded-md border border-rule bg-raise">
          <div className="flex items-center gap-1.5 border-b border-rule-2 py-1.5 pr-1.5 pl-3">
            {railOpen ? (
              <h2 className="flex-1 truncate font-mono text-[0.66rem] tracking-wider text-ink-3 uppercase">
                运行
              </h2>
            ) : null}
            <button
              onClick={() => setRailOpen((open) => !open)}
              aria-label={railOpen ? "收起运行列表" : "展开运行列表"}
              className="mx-auto rounded px-1.5 py-0.5 font-mono text-ink-3 hover:bg-sunk hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
            >
              {railOpen ? "‹" : "›"}
            </button>
          </div>
          {data.runs.map((run) => {
            const current = run.run_id === data.run.run_id;
            return (
              <button
                key={run.run_id}
                onClick={() =>
                  setTarget({ run: run.run_id, episode: run.episode_id })
                }
                aria-current={current}
                title={`${run.run_id} · ${run.events} 事件`}
                className={`grid w-full items-start gap-2 border-b border-rule-2 px-3 py-2 text-left last:border-b-0 hover:bg-sunk focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
                  railOpen ? "grid-cols-[0.55rem_1fr]" : "grid-cols-1 justify-items-center px-0"
                } ${current ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent" : ""}`}
              >
                <span className="mt-1.5 size-[0.55rem] shrink-0 rounded-full bg-ok" />
                {railOpen ? (
                  <span className="min-w-0">
                    <span className="block truncate font-mono text-[0.75rem] font-semibold">
                      {run.run_id}
                    </span>
                    <span className="block truncate font-mono text-[0.68rem] text-ink-3">
                      {run.events} 事件
                    </span>
                  </span>
                ) : null}
              </button>
            );
          })}
        </aside>

        <div className="min-w-0">
          <div className="mb-3 grid gap-px overflow-hidden rounded-md border border-rule bg-rule sm:grid-cols-2 lg:grid-cols-4">
            <Tile
              label="重复外发"
              value={String(h.duplicate_sends)}
              sub={`${h.delivered} 次投递，${h.deduplicated} 次幂等命中`}
              tone={h.duplicate_sends === 0 ? "ok" : "plain"}
            />
            <Tile
              label="人推翻模型"
              value={`${h.human_overruled ?? 0} / ${h.model_advised ?? 0}`}
              sub="模型给了建议，人否了这么多次"
              tone="series"
            />
            <Tile
              label="引用幻觉率"
              value={rate == null ? "—" : rate.toFixed(1)}
              sub={`${h.conclusion_points ?? 0} 个结论，0 个引用未读来源`}
              tone={rate === 0 ? "ok" : "plain"}
            />
            <Tile
              label="审计事件"
              value={String(h.audit_events)}
              sub={`序号 ${data.audit.first}–${data.audit.last}，连续`}
              tone="plain"
            />
          </div>
          <p className="mb-8 max-w-[62ch] text-[0.82rem] text-ink-3">
            这一行就是三十秒能讲完的部分。「人推翻模型」用的是序列色而不是警示色——
            它不是异常，是这套系统的设计目标被观测到了。
          </p>

          <ContextPanel data={data} />
          <ResultPanel data={data} />
          <GatePanel data={data} />
          <OutboxPanel data={data} />
          <AuditPanel data={data} />
          <TokenPanel data={data} />
          <LineagePanel data={data} />
        </div>
      </div>
    </div>
  );
}
