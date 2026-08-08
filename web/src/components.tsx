import type { ReactNode } from "react";

/** A panel. Header carries the name and the metric id it comes from, so a
 *  reader can go find the same number in report.json. */
export function Panel({
  title,
  source,
  why,
  children,
}: {
  title: string;
  source?: string;
  why?: string;
  children: ReactNode;
}) {
  return (
    <article className="mb-4 overflow-hidden rounded-md border border-rule bg-raise">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-rule-2 px-5 py-3">
        <h2 className="text-[0.98rem] font-semibold tracking-tight">{title}</h2>
        {source ? (
          <span className="font-mono text-[0.7rem] text-ink-3">{source}</span>
        ) : null}
      </header>
      <div className="px-5 pt-4 pb-5">
        {why ? (
          <p className="mb-4 max-w-[66ch] text-[0.85rem] text-ink-2">{why}</p>
        ) : null}
        {children}
      </div>
    </article>
  );
}

/** An explanation of why a number matters. Without it a reader who does not
 *  already know the system sees only digits. */
export function Note({
  tone = "accent",
  children,
}: {
  tone?: "accent" | "warn" | "ok";
  children: ReactNode;
}) {
  const tones = {
    accent: "border-accent bg-accent-wash",
    warn: "border-warn bg-warn-wash",
    ok: "border-ok bg-ok-wash",
  };
  return (
    <p
      className={`mt-4 max-w-[68ch] rounded-r border-l-[3px] px-4 py-3 text-[0.86rem] ${tones[tone]}`}
    >
      {children}
    </p>
  );
}

export function Tile({
  label,
  value,
  sub,
  tone = "ok",
}: {
  label: string;
  value: string;
  sub: string;
  tone?: "ok" | "series" | "plain";
}) {
  const valueTone = {
    ok: "text-ok",
    series: "text-series-a",
    plain: "text-ink",
  }[tone];
  return (
    <div className="bg-raise px-4 pt-4 pb-3.5">
      <div className="mb-1.5 font-mono text-[0.72rem] tracking-wider text-ink-3 uppercase">
        {label}
      </div>
      <div
        className={`tabular font-mono text-[1.75rem] leading-none font-semibold tracking-tight ${valueTone}`}
      >
        {value}
      </div>
      <div className="mt-1 text-[0.79rem] text-ink-2">{sub}</div>
    </div>
  );
}

/** One series across ordered stages, so every bar is the same colour: the
 *  differences between stages are the information, and colouring each stage
 *  would suggest they are different kinds of thing. */
export function Funnel({
  rows,
  max,
}: {
  rows: { label: string; value: number; tone?: "plain" | "ok" | "warn" }[];
  max: number;
}) {
  const tones = { plain: "bg-accent", ok: "bg-ok", warn: "bg-warn" };
  return (
    <div className="grid gap-2">
      {rows.map((row) => (
        <div
          key={row.label}
          className="grid grid-cols-[7rem_1fr_3rem] items-center gap-3 sm:grid-cols-[9rem_1fr_3.2rem]"
        >
          <span className="text-[0.82rem] text-ink-2">{row.label}</span>
          <span className="h-5 overflow-hidden rounded bg-sunk">
            <span
              className={`block h-full rounded-r ${tones[row.tone ?? "plain"]}`}
              style={{ width: `${max ? (row.value / max) * 100 : 0}%` }}
            />
          </span>
          <span className="tabular text-right font-mono text-[0.85rem]">
            {row.value}
          </span>
        </div>
      ))}
    </div>
  );
}

export function Guard({
  label,
  value,
  hit = false,
}: {
  label: string;
  value: string;
  hit?: boolean;
}) {
  return (
    <div className="flex items-center gap-2.5 rounded border border-rule-2 px-3 py-2.5">
      <span
        className={`grid size-[1.15rem] shrink-0 place-items-center rounded-full text-[0.72rem] font-bold ${
          hit ? "bg-warn-wash text-warn" : "bg-ok-wash text-ok"
        }`}
      >
        {hit ? value : "✓"}
      </span>
      <span className="leading-tight">
        <span className="block text-[0.79rem]">{label}</span>
        {!hit ? (
          <span className="font-mono text-[0.72rem] text-ink-3">{value}</span>
        ) : null}
      </span>
    </div>
  );
}
