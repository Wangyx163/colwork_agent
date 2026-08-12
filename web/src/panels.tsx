import { useState } from "react";
import { Funnel, Guard, Panel } from "./components";
import type { Observatory } from "./types";

export function ContextPanel({ data }: { data: Observatory }) {
  // These six were literal strings -- five zeros and a one -- with the payload
  // bound to a variable that no rendered element read. The panel therefore
  // reported "授权拒绝 1" against a database holding eleven, and reported five
  // guards as clean without consulting anything. Numbers now come from the
  // payload, and a guard with nothing behind it says so.
  const guards = data.constitution_guards;
  const count = (value: number | null | undefined) =>
    value == null ? null : String(value);
  return (
    <Panel title="Context 与授权">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <Guard label="manifest 缺失" value={count(guards?.missing_manifest)} hit />
        <Guard
          label="越权字段进入上下文"
          value={count(guards?.field_violations)}
          hit
        />
        <Guard
          label="Principal 违规"
          value={count(guards?.principal_violations)}
          hit
        />
        <Guard label="附件二进制外泄" value={count(guards?.binary_leaks)} hit />
        <Guard
          label="输入哈希不匹配"
          value={count(guards?.input_hash_mismatches)}
          hit
        />
        <Guard
          label="授权拒绝"
          value={count(guards?.authorization_rejected)}
          hit={(guards?.authorization_rejected ?? 0) > 0}
        />
        <Guard
          label="鉴权拒绝"
          value={count(guards?.authentication_rejected)}
          hit={(guards?.authentication_rejected ?? 0) > 0}
        />
      </div>
    </Panel>
  );
}

export function ResultPanel({ data }: { data: Observatory }) {
  const r = data.results;
  return (
    <Panel
      title="成果处理漏斗"
    >
      <Funnel
        max={r.received}
        rows={[
          { label: "收到版本", value: r.received },
          { label: "校验未通过", value: r.validation_failed, tone: "warn" },
          { label: "被退回重做", value: r.returned, tone: "warn" },
          { label: "冻结为已验收", value: r.accepted, tone: "ok" },
        ]}
      />
    </Panel>
  );
}

export function GatePanel({ data }: { data: Observatory }) {
  const g = data.human_gates;
  const overruled = Number(g.human_overruled_model_advice ?? 0);
  const advised = Number(g.reviewed_versions_with_model_advice ?? 0);
  const kept = Math.max(0, advised - overruled);
  return (
    <Panel
      title="人工闸口"
    >
      <div className="grid gap-2">
        <div className="flex h-7 overflow-hidden rounded bg-sunk">
          {overruled > 0 ? (
            <span
              className="grid place-items-center bg-series-a font-mono text-[0.74rem] text-white"
              style={{ flex: overruled }}
            >
              推翻 {overruled}
            </span>
          ) : null}
          {kept > 0 ? (
            <span
              className="grid place-items-center bg-accent font-mono text-[0.74rem] text-white"
              style={{ flex: kept }}
            >
              采纳 {kept}
            </span>
          ) : null}
        </div>
        <div className="flex justify-between text-[0.78rem] text-ink-2">
          <span>人推翻模型建议</span>
          <span>人采纳模型建议</span>
        </div>
      </div>
    </Panel>
  );
}

export function OutboxPanel({ data }: { data: Observatory }) {
  const o = data.outbox;
  const max = Math.max(o.created, o.claimed, o.delivered, 1);
  return (
    <Panel
      title="Effect 与 Outbox"
    >
      <Funnel
        max={max}
        rows={[
          { label: "创建", value: o.created },
          { label: "领取", value: o.claimed },
          { label: "重试", value: o.retried, tone: "warn" },
          { label: "幂等命中", value: o.deduplicated, tone: "ok" },
          { label: "投递成功", value: o.delivered, tone: "ok" },
          { label: "死信", value: o.dead_letter, tone: "warn" },
        ]}
      />
    </Panel>
  );
}

export function AuditPanel({ data }: { data: Observatory }) {
  const { first, last, lanes, total } = data.audit;
  const span = Math.max(1, last - first);
  return (
    <Panel
      title={`审计时间线 · ${total} 条`}
    >
      <div className="grid gap-[3px]">
        {lanes.map((lane) => (
          <div
            key={lane.name}
            className="grid grid-cols-[6rem_1fr_2rem] items-center gap-2.5 sm:grid-cols-[9rem_1fr_2.2rem]"
          >
            <span className="truncate text-[0.76rem] text-ink-2">
              {lane.name}
            </span>
            <span className="relative h-[17px] overflow-hidden rounded bg-sunk">
              {lane.events.map((event) => (
                <span
                  key={event.seq}
                  title={`#${event.seq} ${event.event}`}
                  className={`absolute top-0.5 bottom-0.5 w-0.5 rounded-[1px] ${
                    lane.name === "其他" ? "bg-ink-3" : "bg-accent"
                  }`}
                  style={{ left: `${((event.seq - first) / span) * 100}%` }}
                />
              ))}
            </span>
            <span className="tabular text-right font-mono text-[0.74rem] text-ink-3">
              {lane.count}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex justify-between pr-8 pl-[6.5rem] font-mono text-[0.68rem] text-ink-3 sm:pl-[9.5rem]">
        <span>#{first}</span>
        <span>#{Math.round((first + last) / 2)}</span>
        <span>#{last}</span>
      </div>
    </Panel>
  );
}

export function TokenPanel({ data }: { data: Observatory }) {
  const { calls, summary } = data.tokens;
  if (!summary.calls) {
    return (
      <Panel title="Token 消耗" source="model invocation usage">
        <p className="text-[0.9rem] text-ink-2">
          这次运行的 token 消耗是 <b className="font-mono">0</b>。
        </p>
      </Panel>
    );
  }
  const values = calls.map((c) => c.total_tokens);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = Math.max(1, hi - lo);
  const pos = (v: number) => ((v - lo) / span) * 100;
  return (
    <Panel
      title="Token 消耗"
    >
      <div className="relative mb-1 h-[74px]">
        {summary.p25 != null && summary.p75 != null ? (
          <span
            className="absolute top-3 bottom-6 border-x border-accent bg-accent-wash"
            style={{
              left: `${pos(summary.p25)}%`,
              right: `${100 - pos(summary.p75)}%`,
            }}
          />
        ) : null}
        {summary.median != null ? (
          <span
            className="absolute top-1.5 bottom-5 w-0.5 bg-accent"
            style={{ left: `${pos(summary.median)}%` }}
          />
        ) : null}
        {calls.map((call, index) => (
          <span
            key={`${call.sequence_no}-${index}`}
            title={`#${call.sequence_no} ${call.purpose} · ${call.total_tokens}`}
            className={`absolute top-7 -ml-[5.5px] size-[11px] rounded-full border-2 border-raise ${
              summary.outliers.includes(call.total_tokens)
                ? "bg-series-a"
                : "bg-series-b"
            }`}
            style={{ left: `${pos(call.total_tokens)}%` }}
          />
        ))}
        <span className="absolute right-0 bottom-0 left-0 flex justify-between border-t border-rule pt-1 font-mono text-[0.68rem] text-ink-3">
          <span>{lo.toLocaleString()}</span>
          <span>中位 {summary.median?.toLocaleString()}</span>
          <span>{hi.toLocaleString()}</span>
        </span>
      </div>
    </Panel>
  );
}

export function LineagePanel({ data }: { data: Observatory }) {
  const { versions, fields } = data.lineage;
  const [selected, setSelected] = useState(
    () => versions.find((v) => v.contributed)?.version_id ?? "",
  );
  return (
    <Panel
      title="Lineage 回溯"
    >
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="overflow-hidden rounded border border-rule-2">
          <h3 className="border-b border-rule-2 bg-sunk px-3 py-2 text-[0.76rem] font-semibold">
            版本（点选）
          </h3>
          {versions.map((version) => {
            const isSelected = version.version_id === selected;
            return (
              <button
                key={version.version_id}
                onClick={() => setSelected(version.version_id)}
                aria-current={isSelected}
                className={`flex w-full items-center justify-between gap-3 border-b border-rule-2 px-3 py-2 text-left text-[0.79rem] last:border-b-0 hover:bg-sunk focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
                  isSelected ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent" : ""
                } ${version.superseded ? "bg-bad-wash" : ""}`}
              >
                <span className="truncate">
                  {version.title}
                  {version.superseded ? (
                    <b className="text-bad"> （已被替换）</b>
                  ) : null}
                </span>
                <span
                  className={`tabular shrink-0 font-mono text-[0.73rem] ${
                    version.field_count === 0 ? "font-semibold text-bad" : "text-ink-3"
                  }`}
                >
                  {version.field_count} 字段
                </span>
              </button>
            );
          })}
        </div>
        <div className="overflow-hidden rounded border border-rule-2">
          <h3 className="border-b border-rule-2 bg-sunk px-3 py-2 text-[0.76rem] font-semibold">
            终稿字段（高亮＝来自选中版本）
          </h3>
          <div className="max-h-80 overflow-y-auto">
            {fields.length === 0 ? (
              <p className="px-3 py-3 text-[0.79rem] text-ink-3">
                这次运行还没有生成终稿。
              </p>
            ) : (
              fields.map((field) => {
                const fromSelected = field.version_id === selected;
                return (
                  <div
                    key={field.field_path}
                    className={`border-b border-rule-2 px-3 py-1.5 font-mono text-[0.74rem] last:border-b-0 ${
                      fromSelected
                        ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent"
                        : "opacity-30"
                    }`}
                  >
                    {field.field_path}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}
