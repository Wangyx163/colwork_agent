import { useState, type ReactNode } from "react";
import type { Activity, Task } from "../manage-types";
import { formatDay } from "./schedule";

/* ------------------------------------------------------------------ atoms */

export function Chip({
  tone = "plain",
  children,
}: {
  tone?: "plain" | "live" | "warn" | "ok" | "bad";
  children: ReactNode;
}) {
  const tones = {
    plain: "bg-sunk text-ink-2",
    live: "bg-accent-wash text-accent",
    warn: "bg-warn-wash text-warn",
    ok: "bg-ok-wash text-ok",
    bad: "bg-bad-wash text-bad",
  };
  return (
    <span
      className={`rounded-sm px-1.5 py-px font-mono text-[0.68rem] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Button({
  tone = "accent",
  onClick,
  disabled,
  children,
}: {
  tone?: "accent" | "ghost" | "good";
  onClick?: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  // Only the primary action carries a filled background. Four buttons of
  // equal weight is the same as none: the eye has to read all of them before
  // it can pick, which is exactly the work the card is supposed to save.
  const tones = {
    accent: "bg-accent text-white shadow-sm hover:opacity-90",
    good: "bg-ok text-white shadow-sm hover:opacity-90",
    ghost:
      "text-ink-2 hover:bg-sunk hover:text-ink border border-transparent hover:border-rule",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded px-2.5 py-1 text-[0.79rem] transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45 ${tones[tone]}`}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------- status → presentation */

const STATUS_LABEL: Record<string, string> = {
  PENDING_CONFIRMATION: "待确认",
  PENDING_ASSIGNMENT: "等接受",
  NEEDS_REVISION: "被退回",
  TRACKING: "执行中",
  BLOCKED: "受阻",
  PENDING_ACCEPTANCE: "待验收",
  ACCEPTED: "已验收",
  AGGREGATED: "已汇总",
  ARCHIVED: "已归档",
  REJECTED: "已忽略",
};

function statusTone(status: string): "plain" | "live" | "warn" | "ok" | "bad" {
  if (status === "TRACKING") return "live";
  if (status === "PENDING_ASSIGNMENT") return "plain";
  if (status === "PENDING_ACCEPTANCE") return "ok";
  if (status === "BLOCKED" || status === "NEEDS_REVISION") return "warn";
  if (["ACCEPTED", "AGGREGATED", "ARCHIVED"].includes(status)) return "ok";
  return "plain";
}

/** The label on the disclosure names what is behind it.
 *
 *  A single "更多" on every card is the same word for five different things:
 *  a reader cannot tell whether opening it shows a delivery, a rescheduling
 *  history, or nothing worth the click. The stage decides the wording, because
 *  the stage decides the content. */
function disclosureLabel(task: Task): string {
  if (task.status === "PENDING_ACCEPTANCE") return "看正文与附件";
  if (task.status === "TRACKING" || task.status === "BLOCKED")
    return "看执行过程";
  if (task.status === "PENDING_ASSIGNMENT") return "看谁还没接受";
  if (["NEEDS_REVISION", "PENDING_CONFIRMATION"].includes(task.status))
    return "看抽取依据";
  return "看验收内容";
}

/* ------------------------------------------------------- detail sections */

function Line({
  when,
  who,
  title,
  detail,
  tone,
}: {
  when: string;
  who: string;
  title: string;
  detail?: string;
  tone?: "warn" | "ok";
}) {
  const dot =
    tone === "warn" ? "bg-warn" : tone === "ok" ? "bg-ok" : "bg-ink-3";
  return (
    <li className="grid grid-cols-[0.45rem_3.2rem_1fr] items-start gap-2 py-1">
      <span className={`mt-[0.42rem] size-[0.45rem] rounded-full ${dot}`} />
      <span className="tabular mt-px font-mono text-[0.68rem] text-ink-3">
        {formatDay(when)}
      </span>
      <span className="min-w-0">
        <b className="text-[0.79rem] font-semibold">{title}</b>
        <span className="ml-1.5 font-mono text-[0.68rem] text-ink-3">
          {who}
        </span>
        {detail ? (
          <span className="mt-px block text-[0.78rem] text-ink-2">{detail}</span>
        ) : null}
      </span>
    </li>
  );
}

/** Execution: everything that has happened on this task, newest first.
 *
 *  Nothing is filtered out. The card above stays deliberately bare, and this
 *  is where the cost of that is paid back -- progress signals, help requests,
 *  who else is on it, every L1/L2 nudge, and each time the promised date
 *  moved. Splitting rescheduling into its own view made a reader open two
 *  panels to reconstruct one story. */
function ExecutionDetail({ task }: { task: Task }) {
  const lines = task.activity || [];
  const collaborators = (task.collaborators || [])
    .map((person) => person.display_name || person.actor_id)
    .filter(Boolean);
  const nudges = lines.filter((entry) => entry.kind === "INTERVENTION").length;
  const assistance = task.active_assistance;

  return (
    <>
      <dl className="mb-3 grid grid-cols-[4.5rem_1fr] gap-x-3 gap-y-1 text-[0.79rem]">
        <dt className="font-mono text-[0.71rem] text-ink-3">协作者</dt>
        <dd className="text-ink-2">
          {collaborators.length ? collaborators.join("、") : "只有负责人一个人"}
        </dd>
        <dt className="font-mono text-[0.71rem] text-ink-3">排期</dt>
        <dd className="text-ink-2">
          团队要求 <b>{formatDay(task.team_required_by_sim_time)}</b>
          {task.promised_by_sim_time ? (
            <>
              ，本人承诺 <b>{formatDay(task.promised_by_sim_time)}</b>
            </>
          ) : null}
          {task.schedule_risk_reason ? `（${task.schedule_risk_reason}）` : ""}
        </dd>
        {nudges ? (
          <>
            <dt className="font-mono text-[0.71rem] text-ink-3">催办</dt>
            <dd className="text-warn">系统已触达 {nudges} 次</dd>
          </>
        ) : null}
        {assistance ? (
          <>
            <dt className="font-mono text-[0.71rem] text-ink-3">求助</dt>
            <dd className="text-warn">
              {assistance.summary || "有一条求助未解决"}
            </dd>
          </>
        ) : null}
      </dl>

      {lines.length ? (
        <ul className="border-t border-rule-2 pt-2">
          {lines.map((entry, index) => (
            <Line
              key={index}
              when={entry.sim_time}
              who={entry.actor}
              title={commitmentTitle(entry)}
              detail={commitmentDetail(entry)}
              tone={
                entry.kind === "INTERVENTION"
                  ? "warn"
                  : entry.kind === "COMMITMENT" && entry.status === "ACTIVE"
                    ? "ok"
                    : undefined
              }
            />
          ))}
        </ul>
      ) : (
        <p className="border-t border-rule-2 pt-2 text-[0.8rem] text-ink-3">
          派发之后还没有任何动静——这本身就是要催办的理由。
        </p>
      )}
    </>
  );
}

/** A commitment entry says more as a date than as a sentence. */
function commitmentTitle(entry: Activity): string {
  if (entry.kind !== "COMMITMENT") return entry.title;
  return `${entry.title}至 ${formatDay(entry.promised_deadline_sim_time)}`;
}

function commitmentDetail(entry: Activity): string | undefined {
  if (entry.kind !== "COMMITMENT") return entry.detail;
  return entry.status === "SUPERSEDED" ? "后来改掉了" : "当前生效";
}

/** Delivery: the thing being judged, not a log of how it got here. */
function DeliveryDetail({ task }: { task: Task }) {
  const version = task.latest_version || task.current_version;
  if (!version) return <Empty>还没有提交任何版本。</Empty>;
  const payload = version.payload || {};
  const attachments = version.attachment_extractions || [];
  const links = payload.links || [];
  return (
    <>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[0.71rem] text-ink-3">
        <span>版本 {version.received_sequence ?? "—"}</span>
        <span>提交 {formatDay(version.received_sim_time)}</span>
        {version.submitted_by_display_name ? (
          <span>{version.submitted_by_display_name}</span>
        ) : null}
        {version.processing_status &&
        version.processing_status !== "NOT_REQUIRED" ? (
          <span>整理 {version.processing_status}</span>
        ) : null}
      </div>
      {/* A real delivery often puts everything in the summary and a link, and
          leaves `content` empty. Rendering an empty box labelled "正文为空"
          above the actual substance reported a complete submission as a blank
          one, so the body is whichever of these was actually filled in. */}
      {payload.summary ? (
        <p className="mb-2 text-[0.84rem] font-semibold">{payload.summary}</p>
      ) : null}
      {payload.content ? (
        <p className="max-h-56 overflow-y-auto rounded border border-rule-2 bg-ground px-3 py-2 text-[0.82rem] whitespace-pre-wrap">
          {payload.content}
        </p>
      ) : null}
      {payload.completion_note ? (
        <p className="mt-1 text-[0.8rem] text-ink-2">
          {payload.completion_note}
        </p>
      ) : null}
      {!payload.summary && !payload.content && !payload.completion_note ? (
        <p className="text-[0.8rem] text-ink-3">（这一版没有正文）</p>
      ) : null}
      {links.length || attachments.length ? (
        <ul className="mt-2 flex flex-wrap gap-2">
          {links.map((link, index) => (
            <li key={`link-${index}`}>
              <a
                href={link}
                target="_blank"
                rel="noreferrer"
                title={link}
                className="inline-block max-w-[22rem] truncate rounded border border-rule-2 px-2 py-0.5 font-mono text-[0.7rem] text-accent underline"
              >
                🔗 {link}
              </a>
            </li>
          ))}
          {attachments.map((file, index) => (
            <li
              key={`file-${index}`}
              className="rounded border border-rule-2 px-2 py-0.5 font-mono text-[0.7rem] text-ink-2"
            >
              {file.name || "附件"}
              {file.status && file.status !== "EXTRACTED"
                ? ` · ${file.status}`
                : ""}
            </li>
          ))}
        </ul>
      ) : null}
      {version.review_comment ? (
        <p className="mt-2 rounded-r border-l-[3px] border-warn bg-warn-wash px-3 py-2 text-[0.8rem]">
          上一次退回时写的：{version.review_comment}
        </p>
      ) : null}
    </>
  );
}

/** Dispatched, waiting. The only question about this task is who has not
 *  answered yet, so that is the whole panel. */
function AwaitingDetail({ task }: { task: Task }) {
  const assignments = task.current_assignments?.length
    ? task.current_assignments
    : task.assignments || [];
  if (!assignments.length)
    return <Empty>还没有派发记录。</Empty>;
  return (
    <ul>
      {assignments.map((assignment) => {
        const answered = assignment.response_status !== "PENDING";
        return (
          <li
            key={assignment.assignment_id}
            className="flex items-center gap-2 py-1 text-[0.8rem]"
          >
            <span
              className={`size-[0.45rem] rounded-full ${
                answered ? "bg-ok" : "bg-warn"
              }`}
            />
            <span>{assignment.display_name || assignment.actor_id}</span>
            <span className="font-mono text-[0.7rem] text-ink-3">
              {assignment.assignment_role === "OWNER" ? "主负责人" : "协作者"}
            </span>
            <span className="ml-auto font-mono text-[0.7rem] text-ink-3">
              {answered ? "已回应" : "等回应"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Provenance: where this task came from, so a dispatch is not a guess. */
function ProvenanceDetail({ task }: { task: Task }) {
  const meta = (task.proposal_metadata || {}) as Record<string, string>;
  const rows = [
    ["交付物", meta.deliverable || task.deliverable_key],
    ["验收标准", meta.acceptance_criteria],
    ["会议出处", task.source_span],
  ].filter(([, value]) => value);
  return (
    <dl className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-1.5 text-[0.8rem]">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="font-mono text-[0.71rem] text-ink-3">{label}</dt>
          <dd className="text-ink-2">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A finished task: what was accepted, then who accepted it.
 *
 *  Showing only the verdict answered a question nobody asks. "验收通过" is
 *  already on the chip; what a reader opens a finished task for is the thing
 *  that was delivered. */
function OutcomeDetail({ task }: { task: Task }) {
  const review = (task.activity || []).find((entry) => entry.kind === "REVIEW");
  const version = task.current_version || task.latest_version;
  if (!review && !version) return <Empty>没有留下验收记录。</Empty>;
  return (
    <>
      {version ? <DeliveryDetail task={task} /> : null}
      {review ? (
        <ul className={version ? "mt-3 border-t border-rule-2 pt-2" : ""}>
          <Line
            when={review.sim_time}
            who={review.actor}
            title={review.title}
            detail={review.detail}
            tone="ok"
          />
        </ul>
      ) : null}
    </>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="text-[0.8rem] text-ink-3">{children}</p>;
}

/* ------------------------------------------------------------------ card */

export interface CardAction {
  label: string;
  tone?: "accent" | "ghost" | "good";
  run: () => void;
}

export function TaskCard({
  task,
  late,
  lateDays,
  selected,
  actions,
  onSelect,
  cardRef,
  extra,
}: {
  task: Task;
  late?: boolean;
  lateDays?: number;
  selected?: boolean;
  actions: CardAction[];
  onSelect?: () => void;
  cardRef?: (node: HTMLElement | null) => void;
  /** Rendered below the disclosure: an inline form a zone opens on its own
   *  terms, kept out of the card so the card does not learn about forms. */
  extra?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const owner =
    task.assigned_owner_display_name || task.owner_display_name || "未指派";

  let detail: ReactNode;
  if (task.status === "PENDING_ACCEPTANCE") detail = <DeliveryDetail task={task} />;
  else if (task.status === "TRACKING" || task.status === "BLOCKED")
    detail = <ExecutionDetail task={task} />;
  else if (task.status === "PENDING_ASSIGNMENT")
    detail = <AwaitingDetail task={task} />;
  else if (
    ["NEEDS_REVISION", "PENDING_CONFIRMATION"].includes(task.status)
  )
    detail = <ProvenanceDetail task={task} />;
  else detail = <OutcomeDetail task={task} />;

  return (
    <article
      ref={cardRef}
      onClick={onSelect}
      className={`rounded-md border bg-raise px-3.5 py-3 transition-shadow ${
        selected
          ? "border-accent ring-2 ring-accent-wash"
          : "border-rule-2 hover:border-rule"
      } ${onSelect ? "cursor-pointer" : ""}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[0.9rem] font-semibold">{task.title}</span>
        <Chip tone={statusTone(task.status)}>
          {STATUS_LABEL[task.status] || task.status}
        </Chip>
        {late ? <Chip tone="warn">工期冲突</Chip> : null}
        {task.required ? null : <Chip>非必需</Chip>}
      </div>

      <div className="tabular mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[0.71rem] text-ink-3">
        <span>{owner}</span>
        <span>团队 {formatDay(task.team_required_by_sim_time)}</span>
        {task.promised_by_sim_time ? (
          <span>承诺 {formatDay(task.promised_by_sim_time)}</span>
        ) : null}
        {late && lateDays ? (
          <span className="text-warn">晚 {lateDays} 天</span>
        ) : null}
      </div>

      {/* No phantom row when there is nothing to do: a reader looking at
          somebody else's task should see the card end, not an empty ledge
          where a control seems to have failed to render. */}
      <div
        className={`flex flex-wrap items-center gap-2 ${
          actions.length ? "mt-2.5" : "mt-1"
        }`}
      >
        {actions.map((action) => (
          <Button key={action.label} tone={action.tone} onClick={action.run}>
            {action.label}
          </Button>
        ))}
        <button
          onClick={(event) => {
            event.stopPropagation();
            setOpen((value) => !value);
          }}
          aria-expanded={open}
          className="ml-auto rounded px-1 py-0.5 text-[0.76rem] text-ink-3 hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
        >
          {disclosureLabel(task)} {open ? "▴" : "▾"}
        </button>
      </div>

      {open ? (
        <div className="mt-3 border-t border-rule-2 pt-3">{detail}</div>
      ) : null}
      {extra}
    </article>
  );
}
