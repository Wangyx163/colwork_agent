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
  why,
  anchor,
  action,
  tool,
  children,
}: {
  n: string;
  name: string;
  pending: number;
  pendingLabel?: string;
  ownerOnly?: boolean;
  why?: string;
  anchor?: string;
  /** Sits at the right of the header. For the thing this stage lets you start,
   *  which otherwise becomes a card competing with the work itself. */
  action?: ReactNode;
  /** Not a stage the work passes through but a tool you reach for, so it is
   *  drawn as one: no number, a quieter ground, a dashed edge. Mixing the two
   *  treatments made a declaration form look like a queue with nothing in it. */
  tool?: boolean;
  children: ReactNode;
}) {
  return (
    <section
      id={anchor}
      className={`mb-7 overflow-hidden rounded-md scroll-mt-4 ${
        tool
          ? "border border-dashed border-rule-2 bg-ground"
          : "border border-rule bg-raise"
      }`}
    >
      <header
        className={`flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 ${
          tool ? "" : "border-b border-rule-2"
        }`}
      >
        {tool ? null : (
          <span className="font-mono text-[0.7rem] tracking-wider text-accent">
            {n}
          </span>
        )}
        <h2
          className={
            tool
              ? "text-[0.92rem] font-semibold tracking-tight text-ink-2"
              : "text-[1rem] font-semibold tracking-tight"
          }
        >
          {name}
        </h2>
        {tool ? null : (
          <span
            className={`rounded-full px-2 py-px font-mono text-[0.72rem] ${
              pending > 0 ? "bg-accent-wash text-accent" : "bg-sunk text-ink-3"
            }`}
          >
            {pendingLabel ?? `${pending} 项待你处理`}
          </span>
        )}
        {ownerOnly ? (
          <span className="rounded border border-rule px-1.5 py-px text-[0.7rem] text-ink-3">
            仅会议负责人
          </span>
        ) : null}
        {action ? <span className="ml-auto">{action}</span> : null}
      </header>
      <div className="px-4 pt-3.5 pb-4">
        {why ? (
          // Width in em, not ch. `ch` is the advance of "0", so 64ch is about
          // 64 Latin characters and about 32 Chinese ones -- which folded a
          // one-line sentence into two cramped ones. Font size is unchanged;
          // the measure is what was wrong.
          <p className="mb-3 max-w-[58em] text-[0.83rem] leading-relaxed text-ink-3">
            {why}
          </p>
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
