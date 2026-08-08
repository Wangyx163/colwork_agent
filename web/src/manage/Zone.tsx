import type { ReactNode } from "react";

/** One stage of the coordinator's work.
 *
 *  The numbering is the order the work actually moves through -- extracted,
 *  dispatched, delivered, released -- so it carries sequence rather than
 *  decorating the heading. */
export function Zone({
  n,
  name,
  pending,
  pendingLabel,
  ownerOnly,
  history,
  why,
  anchor,
  children,
}: {
  n: string;
  name: string;
  pending: number;
  pendingLabel?: string;
  ownerOnly?: boolean;
  history?: { count: number; open: boolean; toggle: () => void };
  why?: string;
  anchor?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={anchor}
      className="mb-7 overflow-hidden rounded-md border border-rule bg-raise scroll-mt-4"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-rule-2 px-4 py-2.5">
        <span className="font-mono text-[0.7rem] tracking-wider text-accent">
          {n}
        </span>
        <h2 className="text-[1rem] font-semibold tracking-tight">{name}</h2>
        <span
          className={`rounded-full px-2 py-px font-mono text-[0.72rem] ${
            pending > 0
              ? "bg-accent-wash text-accent"
              : "bg-sunk text-ink-3"
          }`}
        >
          {pendingLabel ?? `${pending} 项待你处理`}
        </span>
        {ownerOnly ? (
          <span className="rounded border border-rule px-1.5 py-px text-[0.7rem] text-ink-3">
            仅会议负责人
          </span>
        ) : null}
        {history ? (
          <button
            onClick={history.toggle}
            aria-pressed={history.open}
            className="ml-auto inline-flex items-center gap-1.5 rounded border border-rule px-2 py-0.5 text-[0.76rem] text-ink-2 hover:bg-sunk hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            {history.open ? "← 返回待办" : `🕐 历史 ${history.count}`}
          </button>
        ) : null}
      </header>
      <div className="px-4 pt-3.5 pb-4">
        {why ? (
          <p className="mb-3 max-w-[64ch] text-[0.83rem] text-ink-3">{why}</p>
        ) : null}
        {children}
      </div>
    </section>
  );
}

export function Blank({ children }: { children: ReactNode }) {
  return (
    <p className="rounded border border-dashed border-rule px-4 py-6 text-center text-[0.83rem] text-ink-3">
      {children}
    </p>
  );
}
