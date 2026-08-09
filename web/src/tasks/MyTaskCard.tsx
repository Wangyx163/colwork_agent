import { useState, type ReactNode } from "react";
import { messageId, postJson } from "../api";
import type { ManageState, Task, Vocabulary } from "../manage-types";
import { formatDay } from "../manage/schedule";
import { Button, Chip, ContributionPanel, TaskCard } from "../manage/TaskCard";
import { AssistancePanel } from "./AssistancePanel";
import { useDraft } from "./useDraft";

export type Act = (run: () => Promise<unknown>, done: string) => Promise<void>;

/* The lists themselves come from the server, in `state.vocabulary`.
 *
 * A return reason recorded here is read back from a Feishu card, and the
 * domain refuses a signal or a category outside its own set -- so repeating
 * either list in TypeScript would let a button offer something the server
 * rejects. Only the wording is local, because the server stores codes. */
const SIGNAL_LABEL: Record<string, string> = {
  ON_TRACK: "进行中",
  AT_RISK: "有风险",
  BLOCKED: "被卡住",
  WAITING_INPUT: "等别人",
  READY_TO_SUBMIT: "快好了",
};

/** The last signal on a task is not only ever a quick status: accepting a
 *  dispatch, submitting, asking for help and moving a promise all record one.
 *  Without these the card printed ARTIFACT_SUBMITTED at the reader. */
const EVENT_LABEL: Record<string, string> = {
  ...SIGNAL_LABEL,
  CLAIMED: "已认领",
  ASSIGNMENT_ACCEPTED: "已接受派发",
  ARTIFACT_SUBMITTED: "已提交成果",
  ASSISTANCE_REQUESTED: "发出了求助",
  PROMISE_REVISED: "改了承诺时间",
};

const HELP_LABEL: Record<string, string> = {
  EXPERTISE: "需要专业意见",
  CAPACITY: "人手不够",
  DEPENDENCY: "等上游",
  DECISION: "需要拍板",
  OTHER: "其它",
};

type Panel = "" | "return" | "commit" | "help" | "submit" | "amend";

export function MyTaskCard({
  task,
  state,
  act,
  selected,
  cardRef,
  onSelect,
}: {
  task: Task;
  state: ManageState;
  act: Act;
  selected: boolean;
  cardRef: (node: HTMLElement | null) => void;
  onSelect: () => void;
}) {
  const [panel, setPanel] = useState<Panel>("");
  const me = state.principal.actor_id;
  const meta = (task.proposal_metadata || {}) as Record<string, string>;
  const owner = task.owner_actor_id === me;
  const awaiting = task.my_assignment?.response_status === "PENDING";
  const late =
    Boolean(task.promised_by_sim_time) &&
    Boolean(task.team_required_by_sim_time) &&
    task.promised_by_sim_time! > task.team_required_by_sim_time!;

  // Call sites carry the whole path rather than a verb the helper glues on:
  // a URL assembled from a variable cannot be found by grep, and the check
  // that every server route is reachable from a page reads the source.
  const post = (url: string, body: Record<string, unknown>, done: string) =>
    void act(async () => {
      await postJson(url, body);
      setPanel("");
    }, done);
  const id = task.action_item_id;

  const toggle = (next: Panel) =>
    setPanel((current) => (current === next ? "" : next));

  /* One decision at a time. While a dispatch is unanswered nothing else on
     the task is actionable -- accepting is what creates the commitment the
     other controls operate on. */
  const actions = awaiting
    ? [
        {
          label: "接受",
          tone: "good" as const,
          run: () =>
            post(
              `/api/action-items/${id}/assignment-response`,
              {
                decision: "ACCEPT",
                response_message: "",
                message_id: messageId("accept"),
              },
              "已接受，这条任务进入执行",
            ),
        },
        {
          label: "退回重改",
          tone: "ghost" as const,
          run: () => toggle("return"),
        },
      ]
    : [
        ...(task.can_contribute
          ? [
              {
                label: "提交成果",
                tone: "accent" as const,
                run: () => toggle("submit"),
              },
            ]
          : []),
        ...(owner
          ? [
              { label: "报进展", tone: "ghost" as const, run: () => toggle("commit") },
              { label: "求助", tone: "ghost" as const, run: () => toggle("help") },
              {
                label: "改任务说明",
                tone: "ghost" as const,
                run: () => toggle("amend"),
              },
            ]
          : []),
      ];

  return (
    <TaskCard
      task={task}
      late={late}
      selected={selected}
      cardRef={cardRef}
      onSelect={onSelect}
      actions={actions}
      extra={
        <>
          {task.last_owner_signal ? (
            <p className="mt-2 flex flex-wrap items-center gap-2 font-mono text-[0.71rem] text-ink-3">
              <Chip>
                {EVENT_LABEL[task.last_owner_signal.signal_type] ||
                  task.last_owner_signal.signal_type}
              </Chip>
              {formatDay(task.last_owner_signal.signal_at)}
              {task.last_owner_signal.note
                ? ` · ${task.last_owner_signal.note}`
                : ""}
            </p>
          ) : null}

          <AssistancePanel task={task} me={me} act={act} />

          {owner ? (
            <ContributionPanel
              contributions={(task.contribution_versions || []).filter(
                (version) => version.contribution_status === "AWAITING_OWNER",
              )}
              onDecide={(versionId, decision, comment) =>
                post(
                  `/api/artifact-versions/${versionId}/contribution`,
                  { decision, comment, message_id: messageId("contribution") },
                  decision === "REQUEST_REVISION"
                    ? "已请对方再改"
                    : "已处理这份材料",
                )
              }
            />
          ) : null}

          {panel === "return" ? (
            <ReturnPanel
              vocabulary={state.vocabulary}
              onCancel={() => setPanel("")}
              onSend={(reason) =>
                post(
                  `/api/action-items/${id}/assignment-response`,
                  {
                    decision: "RETURN_FOR_REVISION",
                    response_message: reason,
                    message_id: messageId("return"),
                  },
                  "已退回，负责人会看到你的原因",
                )
              }
            />
          ) : null}

          {panel === "commit" ? (
            <SignalPanel
              task={task}
              signals={state.vocabulary.quick_signals}
              onCancel={() => setPanel("")}
              onSignal={(signal, note) =>
                post(
                  `/api/action-items/${id}/signal`,
                  {
                    signal_type: signal,
                    note,
                    message_id: messageId("signal"),
                  },
                  "进展已记录，负责人能看到",
                )
              }
              onReschedule={(date, reason) =>
                post(
                  `/api/action-items/${id}/personal-commitment`,
                  {
                    proposed_deadline_sim_time: `${date}T17:00:00+10:00`,
                    reason,
                    message_id: messageId("commit"),
                  },
                  "承诺时间已更新",
                )
              }
            />
          ) : null}

          {panel === "help" ? (
            <HelpPanel
              state={state}
              onCancel={() => setPanel("")}
              onSend={(target, category, summary) =>
                post(
                  `/api/action-items/${id}/assistance`,
                  {
                    target_actor_id: target,
                    category,
                    summary,
                    message_id: messageId("help"),
                  },
                  "求助已发出，对方会收到提示",
                )
              }
            />
          ) : null}

          {panel === "submit" ? (
            <SubmitPanel
              draftKey={task.action_item_id}
              limits={state.vocabulary}
              onCancel={() => setPanel("")}
              onSend={(summary, content, files, clear) =>
                void act(async () => {
                  await postJson(
                    `/api/action-items/${task.action_item_id}/submit`,
                    {
                      delivery: { summary, content, files },
                      message_id: messageId("submit"),
                    },
                  );
                  // Only once it is safely on the server: clearing on submit
                  // would throw the text away if the request failed.
                  clear();
                  setPanel("");
                }, "已提交，等负责人验收")
              }
            />
          ) : null}

          {panel === "amend" ? (
            <AmendPanel
              title={task.title}
              deliverable={meta.deliverable || ""}
              onCancel={() => setPanel("")}
              onSend={(title, deliverable) =>
                post(
                  `/api/action-items/${id}/amend`,
                  { title, deliverable, message_id: messageId("amend") },
                  "已更新，协作的人会收到提示",
                )
              }
            />
          ) : null}
        </>
      }
    />
  );
}

/* ------------------------------------------------------------- panels */

function Frame({
  tone = "plain",
  children,
}: {
  tone?: "plain" | "warn";
  children: ReactNode;
}) {
  return (
    <div
      className={`mt-3 grid gap-2.5 rounded-md border px-3 py-3 ${
        tone === "warn" ? "border-warn bg-warn-wash" : "border-rule-2 bg-ground"
      }`}
    >
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-[0.79rem]">
      {label}
      {children}
    </label>
  );
}

/** The server takes attachments as data URLs, so the file never leaves the
 *  browser as anything else. */
function asDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

const INPUT =
  "rounded border border-rule bg-raise px-2 py-1 text-[0.82rem] focus-visible:outline-2 focus-visible:outline-accent";

function ReturnPanel({
  vocabulary,
  onCancel,
  onSend,
}: {
  vocabulary: Vocabulary;
  onCancel: () => void;
  onSend: (reason: string) => void;
}) {
  const presets = vocabulary.return_reasons;
  const other = vocabulary.other_return_reason;
  const [picked, setPicked] = useState(presets[0] ?? other);
  const [written, setWritten] = useState("");
  const custom = picked === other;
  const reason = custom ? written.trim() : picked;

  return (
    <Frame tone="warn">
      <p className="text-[0.79rem]">退回原因（负责人会看到）</p>
      {[...presets, other].map((option) => (
        <label
          key={option}
          className="flex items-start gap-2 text-[0.81rem]"
        >
          <input
            type="radio"
            name="return"
            className="mt-1"
            checked={picked === option}
            onChange={() => setPicked(option)}
          />
          {option}
        </label>
      ))}
      {custom ? (
        <textarea
          rows={2}
          value={written}
          autoFocus
          onChange={(event) => setWritten(event.target.value)}
          className={INPUT}
        />
      ) : null}
      <div className="flex gap-2">
        <Button disabled={!reason} onClick={() => onSend(reason)}>
          确认退回
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          取消
        </Button>
      </div>
    </Frame>
  );
}

function SignalPanel({
  task,
  signals,
  onCancel,
  onSignal,
  onReschedule,
}: {
  task: Task;
  signals: string[];
  onCancel: () => void;
  onSignal: (signal: string, note: string) => void;
  onReschedule: (date: string, reason: string) => void;
}) {
  const [signal, setSignal] = useState(signals[0] ?? "ON_TRACK");
  const [note, setNote] = useState("");
  const [date, setDate] = useState(
    (task.promised_by_sim_time || "").slice(0, 10),
  );
  const [why, setWhy] = useState("");
  const moved = date && date !== (task.promised_by_sim_time || "").slice(0, 10);

  return (
    <Frame>
      <Field label="现在是什么状态">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {signals.map((option) => (
            <label
              key={option}
              className="inline-flex items-center gap-1.5 text-[0.81rem]"
            >
              <input
                type="radio"
                name={`signal-${task.action_item_id}`}
                checked={signal === option}
                onChange={() => setSignal(option)}
              />
              {SIGNAL_LABEL[option] ?? option}
            </label>
          ))}
        </div>
      </Field>
      <Field label="补充一句（可选）">
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          className={INPUT}
        />
      </Field>
      <div>
        <Button onClick={() => onSignal(signal, note)}>报进展</Button>
      </div>

      <div className="mt-1 border-t border-rule-2 pt-2.5">
        <p className="mb-2 text-[0.79rem] text-ink-3">
          做不完就改自己的承诺时间。团队要求的
          <b> {formatDay(task.team_required_by_sim_time)} </b>
          不会因此改变——两者不一致会被标成工期冲突，让负责人看见。
        </p>
        <div className="grid gap-2">
          <Field label="我能交付的时间">
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className={INPUT}
            />
          </Field>
          <Field label="原因">
            <input
              value={why}
              onChange={(event) => setWhy(event.target.value)}
              className={INPUT}
            />
          </Field>
          <div className="flex gap-2">
            <Button
              disabled={!moved || !why.trim()}
              onClick={() => onReschedule(date, why)}
            >
              改承诺时间
            </Button>
            <Button tone="ghost" onClick={onCancel}>
              收起
            </Button>
          </div>
        </div>
      </div>
    </Frame>
  );
}

function HelpPanel({
  state,
  onCancel,
  onSend,
}: {
  state: ManageState;
  onCancel: () => void;
  onSend: (target: string, category: string, summary: string) => void;
}) {
  const [target, setTarget] = useState("");
  const [category, setCategory] = useState(
    state.vocabulary.assistance_categories[0] ?? "OTHER",
  );
  const [summary, setSummary] = useState("");

  return (
    <Frame>
      <Field label="找谁">
        <select
          value={target}
          onChange={(event) => setTarget(event.target.value)}
          className={INPUT}
        >
          <option value="">请选择</option>
          {state.participants
            .filter((person) => person.actor_id !== state.principal.actor_id)
            .map((person) => (
              <option key={person.actor_id} value={person.actor_id}>
                {person.display_name}
              </option>
            ))}
        </select>
      </Field>
      <Field label="卡在哪">
        <select
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          className={INPUT}
        >
          {state.vocabulary.assistance_categories.map((option) => (
            <option key={option} value={option}>
              {HELP_LABEL[option] ?? option}
            </option>
          ))}
        </select>
      </Field>
      <Field label="说清楚要什么">
        <textarea
          rows={2}
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          className={INPUT}
        />
      </Field>
      <div className="flex gap-2">
        <Button
          disabled={!target || !summary.trim()}
          onClick={() => onSend(target, category, summary)}
        >
          发出求助
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          取消
        </Button>
      </div>
    </Frame>
  );
}

interface Upload {
  name: string;
  type: string;
  size: number;
  data: string;
}

function SubmitPanel({
  draftKey,
  limits,
  onCancel,
  onSend,
}: {
  draftKey: string;
  limits: Vocabulary;
  onCancel: () => void;
  onSend: (
    summary: string,
    content: string,
    files: Upload[],
    clear: () => void,
  ) => void;
}) {
  const [summary, setSummary, clearSummary] = useDraft(`${draftKey}:summary`);
  const [content, setContent, clearContent] = useDraft(`${draftKey}:content`);
  // Files are not drafted: they live in the picker until the submission goes,
  // because a megabyte of base64 in localStorage would fill the quota and
  // silently break the text drafts that matter more.
  const [files, setFiles] = useState<Upload[]>([]);
  const [problem, setProblem] = useState("");
  const bytes = files.reduce((total, file) => total + file.size, 0);
  const clear = () => {
    clearSummary();
    clearContent();
    setFiles([]);
  };

  const add = async (picked: FileList | null) => {
    if (!picked?.length) return;
    setProblem("");
    const chosen: Upload[] = [];
    for (const file of Array.from(picked)) {
      chosen.push({
        name: file.name,
        type: file.type || "application/octet-stream",
        size: file.size,
        data: await asDataUrl(file),
      });
    }
    const merged = [...files, ...chosen];
    // Checked here as well as on the server, so somebody learns before
    // spending a minute picking files rather than after a 413.
    if (merged.length > limits.max_attachment_count) {
      setProblem(`最多 ${limits.max_attachment_count} 个附件`);
      return;
    }
    const total = merged.reduce((sum, file) => sum + file.size, 0);
    if (total > limits.max_attachment_bytes) {
      setProblem(
        `附件总大小上限 ${Math.round(limits.max_attachment_bytes / (1024 * 1024))} MB`,
      );
      return;
    }
    setFiles(merged);
  };

  return (
    <Frame>
      <Field label="一句话说明交付了什么">
        <input
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          className={INPUT}
        />
      </Field>
      <Field label="正文">
        <textarea
          rows={5}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          className={INPUT}
        />
      </Field>
      <Field label={`附件（最多 ${limits.max_attachment_count} 个）`}>
        <input
          type="file"
          multiple
          onChange={(event) => {
            void add(event.target.files);
            event.target.value = "";
          }}
          className="text-[0.78rem]"
        />
      </Field>
      {files.length ? (
        <ul className="grid gap-1">
          {files.map((file, index) => (
            <li
              key={`${file.name}-${index}`}
              className="flex items-center gap-2 text-[0.78rem]"
            >
              <span className="flex-1 truncate">{file.name}</span>
              <span className="font-mono text-[0.7rem] text-ink-3">
                {Math.max(1, Math.round(file.size / 1024))} KB
              </span>
              <button
                onClick={() =>
                  setFiles((current) =>
                    current.filter((_, at) => at !== index),
                  )
                }
                className="font-mono text-[0.72rem] text-ink-3 underline hover:text-bad"
              >
                移除
              </button>
            </li>
          ))}
          <li className="font-mono text-[0.72rem] text-ink-3">
            共 {Math.max(1, Math.round(bytes / 1024))} KB
          </li>
        </ul>
      ) : null}
      {problem ? (
        <p className="text-[0.78rem] text-bad">{problem}</p>
      ) : null}
      <p className="text-[0.75rem] text-ink-3">
        摘要和正文都必填——缺一样校验会判不通过，任务不会进入待验收。写到一半刷新也不会丢（附件除外）。
        PDF / Word / Excel / PPT 的正文会被读出来，供验收时核对。
      </p>
      <div className="flex gap-2">
        <Button
          disabled={!summary.trim() || !content.trim()}
          onClick={() => onSend(summary, content, files, clear)}
        >
          提交
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          取消
        </Button>
      </div>
    </Frame>
  );
}

function AmendPanel({
  title: initialTitle,
  deliverable: initialDeliverable,
  onCancel,
  onSend,
}: {
  title: string;
  deliverable: string;
  onCancel: () => void;
  onSend: (title: string, deliverable: string) => void;
}) {
  const [title, setTitle] = useState(initialTitle);
  const [deliverable, setDeliverable] = useState(initialDeliverable);

  return (
    <Frame>
      <p className="text-[0.79rem] text-ink-3">
        只改名称和说明。负责人、协作者、团队要求的时间都不会动。
      </p>
      <Field label="任务名称">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          className={INPUT}
        />
      </Field>
      <Field label="任务说明">
        <textarea
          rows={3}
          value={deliverable}
          onChange={(event) => setDeliverable(event.target.value)}
          className={INPUT}
        />
      </Field>
      <div className="flex gap-2">
        <Button
          disabled={!title.trim() || !deliverable.trim()}
          onClick={() => onSend(title, deliverable)}
        >
          保存并通知
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          取消
        </Button>
      </div>
    </Frame>
  );
}
