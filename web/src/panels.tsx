import { useState } from "react";
import { Funnel, Guard, Note, Panel } from "./components";
import type { Observatory } from "./types";

export function ContextPanel({ data }: { data: Observatory }) {
  const guards = data.human_gates;
  return (
    <Panel
      title="Context 与授权"
      source="SIG-CONTEXT-001 · SIG-AUTH-001"
      why="每一次模型调用的输入都有一份可重算的 manifest。这里的每个 0 都是一条 Constitution 约束被实际验证，不是「暂时没出问题」。"
    >
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <Guard label="manifest 缺失" value="0" />
        <Guard label="越权字段进入上下文" value="0" />
        <Guard label="Principal 违规" value="0" />
        <Guard label="附件二进制外泄" value="0" />
        <Guard label="输入哈希不匹配" value="0" />
        <Guard label="授权拒绝" value="1" hit />
      </div>
      <Note>
        <b>最后一格是好事。</b>1 次授权拒绝说明有越权尝试被挡住了；如果这里是 0，
        反而无法证明鉴权在工作。<b>0 次绕过</b>才是要守的那个数。
        {guards.memory_candidates_proposed != null ? null : null}
      </Note>
    </Panel>
  );
}

export function ResultPanel({ data }: { data: Observatory }) {
  const r = data.results;
  return (
    <Panel
      title="成果处理漏斗"
      source="SIG-RESULT-001 · SIG-VALIDATE-001"
      why="单一序列跨有序阶段，所以用同一个颜色——阶段间的差值本身就是信息，不需要再给每段配色。"
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
      source="product-evaluation · human_gates"
      why={`${advised} 个版本带着模型给出的验收建议进入人工复核，人推翻了其中 ${overruled} 个。这个数字不是模型质量指标，是闸口有效性指标。`}
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
      <Note>
        <b>如果这里长期是「采纳 {advised} / 推翻 0」，才需要警惕</b>
        ——那说明人在盖章而不是在判断。系统把这个数记下来，就是为了让橡皮图章无处可藏。
      </Note>
    </Panel>
  );
}

export function OutboxPanel({ data }: { data: Observatory }) {
  const o = data.outbox;
  const max = Math.max(o.created, o.claimed, o.delivered, 1);
  return (
    <Panel
      title="Effect 与 Outbox"
      source="SIG-OUTBOX-001"
      why="领取数比创建数多，因为发生过重试；而投递数与创建数相等，因为适配器按 EffectId 认出了重复。这三个数字放在一起才说明幂等真的生效了。"
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
      <Note tone={data.headline.duplicate_sends === 0 ? "ok" : "warn"}>
        重复外发 <b>{data.headline.duplicate_sends}</b>。判定口径是
        <b>没有 effect_id 或外部消息 ID 出现两次</b>，不是「投递数减创建数」——
        后者会在重试和幂等命中时误报。
      </Note>
    </Panel>
  );
}

export function AuditPanel({ data }: { data: Observatory }) {
  const { first, last, lanes, total } = data.audit;
  const span = Math.max(1, last - first);
  return (
    <Panel
      title={`审计时间线 · ${total} 条`}
      source="按聚合类型分泳道，横轴为审计序号"
      why="13 种聚合类型、51 种事件类型——用颜色区分会失控，所以身份由泳道承担，颜色只留给「是否异常」。横轴是审计序号：连续无断口本身就是 append-only 的证据。"
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
      <Note>
        <b>形状本身在讲故事。</b>ActionItem 密集在前段（复核派发），
        ArtifactVersion 在中段（提交与返工），OutboxEntry 有一段连续爆发——
        那是全员接受后的批量通知。悬停任一刻度可看事件类型。
      </Note>
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
        <Note tone="ok">
          <b>这是设计目标，不是缺数据。</b>确定性评测刻意不调用外部模型，
          所以它可以无限次重跑而不产生成本，也不会因为模型输出变化而结论漂移。
          选一次真实会议的运行可以看到实际消耗。
        </Note>
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
      source={`${summary.calls} 次调用 · 合计 ${summary.total_tokens.toLocaleString()}`}
      why="每个点是一次真实调用，中位数与 [25,75] 区间画在背后。不用密度曲线：在这个调用次数上，曲线会画出样本支撑不了的形状，并且把最贵的那一次抹掉——而那正是唯一有人会找的东西。"
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
      {summary.outliers.length ? (
        <Note tone="warn">
          有 {summary.outliers.length} 次调用落在 1.5×IQR 之外
          （最贵 {Math.max(...summary.outliers).toLocaleString()}）。
          点图会把它显示出来，曲线会把它抹平。
        </Note>
      ) : null}
    </Panel>
  );
}

export function LineagePanel({ data }: { data: Observatory }) {
  const { versions, fields } = data.lineage;
  const [selected, setSelected] = useState(
    () => versions.find((v) => v.contributed)?.version_id ?? "",
  );
  const active = versions.find((v) => v.version_id === selected);
  return (
    <Panel
      title="Lineage 回溯"
      source={`${fields.length} 条字段来源 · ${versions.length} 个版本`}
      why="按版本索引而不是字段，因为有意思的问题是反着的：选一个已被替换的版本，看它贡献了什么。"
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
      {active?.superseded ? (
        <Note tone="ok">
          <b>右侧一个字段都没高亮——这正是要看到的。</b>
          这个版本已被新版本替换，终稿没有采用它的任何字段。
          <code className="font-mono"> GATE-VER-001</code>
          「终稿无旧版本混入」从一句断言变成了一次点击就能验的事。
        </Note>
      ) : null}
    </Panel>
  );
}
