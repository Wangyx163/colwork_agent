import { useEffect, useRef, useState } from "react";
import type { ManageState, Notice, Task } from "../manage-types";

const SEEN_KEY = "seenNotice";

/** What is waiting on this person, in one place.
 *
 *  Two different things share the badge and they are not interchangeable: a
 *  dispatch is a question they have to answer, and an amended description is
 *  something they have to know. The first stays until it is answered, because
 *  the domain tracks it; the second clears once it has been looked at, and
 *  that "looked at" lives in this browser -- there is no read model in the
 *  domain, and inventing one would put a UI concern into the audit trail. */
export function Bell({
  state,
  onFocus,
}: {
  state: ManageState;
  onFocus: (actionItemId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState(
    () => localStorage.getItem(`${SEEN_KEY}:${state.principal.actor_id}`) || "",
  );
  const box = useRef<HTMLDivElement>(null);

  const pending = state.tasks.filter(
    (task) => task.my_assignment?.response_status === "PENDING",
  );
  const informational = (state.notices || []).filter(
    (notice) => !notice.decides,
  );
  const cut = informational.findIndex((notice) => notice.notice_id === seen);
  const unseen = cut < 0 ? informational : informational.slice(0, cut);
  const total = pending.length + unseen.length;

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const toggle = () => {
    setOpen((value) => {
      if (!value && informational.length) {
        const newest = informational[0].notice_id;
        localStorage.setItem(
          `${SEEN_KEY}:${state.principal.actor_id}`,
          newest,
        );
        setSeen(newest);
      }
      return !value;
    });
  };

  return (
    <div ref={box} className="relative">
      <button
        onClick={toggle}
        aria-label={`待我处理 ${total} 项`}
        aria-expanded={open}
        className="relative rounded border border-rule px-2.5 py-1 text-[0.9rem] hover:bg-sunk focus-visible:outline-2 focus-visible:outline-accent"
      >
        🔔
        {total ? (
          <span className="absolute -top-1.5 -right-1.5 min-w-[1.15rem] rounded-full bg-bad px-1 text-center font-mono text-[0.65rem] leading-[1.15rem] text-white">
            {total}
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="absolute top-full right-0 z-20 mt-2 max-h-[70vh] w-[min(24rem,calc(100vw-2rem))] overflow-y-auto rounded-md border border-rule bg-raise p-3 shadow-lg">
          <p className="mb-2 flex items-baseline justify-between text-[0.85rem] font-semibold">
            待我处理
            <span className="font-mono text-[0.72rem] font-normal text-ink-3">
              {total} 项
            </span>
          </p>

          {pending.map((task) => (
            <Row
              key={task.action_item_id}
              title={task.title}
              tag={
                task.my_assignment?.assignment_role === "OWNER"
                  ? "派给我"
                  : "邀我协作"
              }
              tone="ask"
              body={
                task.my_assignment?.assignment_message || "负责人没有补充留言"
              }
              onGo={() => {
                onFocus(task.action_item_id);
                setOpen(false);
              }}
            />
          ))}

          {unseen.map((notice) => (
            <Row
              key={notice.notice_id}
              title={notice.title}
              tag="通知"
              tone="tell"
              body={notice.summary}
              fields={notice.fields}
              onGo={() => {
                onFocus(notice.action_item_id);
                setOpen(false);
              }}
            />
          ))}

          {!total ? (
            <p className="py-6 text-center text-[0.83rem] text-ink-3">
              没有需要你处理的事
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Row({
  title,
  tag,
  tone,
  body,
  fields,
  onGo,
}: {
  title: string;
  tag: string;
  tone: "ask" | "tell";
  body: string;
  fields?: { label: string; value: string }[];
  onGo: () => void;
}) {
  return (
    <article className="mb-2 rounded border border-rule-2 px-3 py-2 last:mb-0">
      <div className="flex items-baseline justify-between gap-2">
        <b className="text-[0.84rem]">{title}</b>
        <span
          className={`shrink-0 rounded-sm px-1.5 py-px font-mono text-[0.66rem] ${
            tone === "ask"
              ? "bg-accent-wash text-accent"
              : "bg-sunk text-ink-3"
          }`}
        >
          {tag}
        </span>
      </div>
      {body ? (
        <p className="mt-0.5 text-[0.79rem] text-ink-2">{body}</p>
      ) : null}
      {(fields || []).map((field, index) => (
        <p key={index} className="font-mono text-[0.71rem] text-ink-3">
          {field.label}：{field.value}
        </p>
      ))}
      <button
        onClick={onGo}
        className="mt-1.5 text-[0.78rem] text-accent underline focus-visible:outline-2 focus-visible:outline-accent"
      >
        去处理
      </button>
    </article>
  );
}

export type { Notice, Task };
