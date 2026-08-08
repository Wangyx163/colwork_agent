import type { Strip } from "./schedule";

/** The schedule strip.
 *
 *  Deliberately thin: 5px bars on an 18px track. A thicker bar would carry no
 *  more information -- the meaning is in where the marks sit relative to the
 *  team tick, not in how much ink each row uses. */
export function ScheduleStrip({
  strip,
  selected,
  onPick,
}: {
  strip: Strip;
  selected: string | null;
  onPick: (actionItemId: string) => void;
}) {
  if (!strip.rows.length) return null;

  return (
    <div className="mb-4 overflow-hidden rounded-md border border-rule-2">
      <div className="grid grid-cols-[7.5rem_1fr] gap-3 border-b border-rule-2 bg-sunk px-3 pt-1.5 pb-1 sm:grid-cols-[11rem_1fr]">
        <span />
        <span className="relative h-4">
          {strip.ticks.map((tick) => (
            <span
              key={tick.label + tick.at}
              className="absolute top-0 -translate-x-1/2 font-mono text-[0.65rem] text-ink-3"
              style={{ left: `${tick.at * 100}%` }}
            >
              {tick.label}
            </span>
          ))}
        </span>
      </div>

      {strip.rows.map((row) => {
        const id = row.task.action_item_id;
        const current = selected === id;
        const owner =
          row.task.assigned_owner_display_name ||
          row.task.owner_display_name ||
          "未指派";
        // The promise sits at the far end of the bar; the part beyond the team
        // tick is the overrun, and it is the only thing painted in a warn hue.
        const left = Math.min(row.start, row.promised ?? row.start);
        const end = row.promised ?? row.start;
        const split = row.late && row.required !== null ? row.required : end;

        return (
          <button
            key={id}
            onClick={() => onPick(id)}
            aria-current={current}
            title={`${row.task.title} · ${owner}`}
            className={`grid w-full grid-cols-[7.5rem_1fr] items-center gap-3 border-b border-rule-2 px-3 py-1.5 text-left last:border-b-0 hover:bg-sunk focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent sm:grid-cols-[11rem_1fr] ${
              current ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent" : ""
            }`}
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="truncate text-[0.79rem]">{row.task.title}</span>
              <span className="shrink-0 font-mono text-[0.66rem] text-ink-3">
                {owner}
              </span>
            </span>

            <span className="relative h-[18px]">
              <span className="absolute top-2 right-0 left-0 h-px bg-rule-2" />

              {strip.now !== null ? (
                <span
                  className="absolute top-0 bottom-0 w-px bg-series-b opacity-60"
                  style={{ left: `${strip.now * 100}%` }}
                />
              ) : null}

              {row.ghosts.map((ghost, index) => (
                <span
                  key={index}
                  aria-hidden
                  className="absolute top-[7px] h-[3px] rounded-sm opacity-50"
                  style={{
                    left: `${Math.min(row.start, ghost) * 100}%`,
                    width: `${Math.abs(ghost - row.start) * 100}%`,
                    background:
                      "repeating-linear-gradient(90deg,currentColor 0 3px,transparent 3px 6px)",
                  }}
                />
              ))}

              <span
                className={`absolute top-[6px] h-[5px] rounded-sm ${
                  row.done ? "bg-ok" : "bg-accent"
                }`}
                style={{
                  left: `${left * 100}%`,
                  width: `${Math.max(0, split - left) * 100}%`,
                }}
              />
              {row.late ? (
                <span
                  className="absolute top-[6px] h-[5px] rounded-r-sm bg-warn"
                  style={{
                    left: `${split * 100}%`,
                    width: `${Math.max(0, end - split) * 100}%`,
                  }}
                />
              ) : null}

              {row.required !== null ? (
                <span
                  className="absolute top-px bottom-px w-[2px] rounded-sm bg-ink-2"
                  style={{ left: `${row.required * 100}%` }}
                />
              ) : null}
            </span>
          </button>
        );
      })}

      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-rule-2 bg-sunk px-3 py-2 text-[0.71rem] text-ink-2">
        <Key className="h-[5px] w-3.5 rounded-sm bg-accent">个人承诺区间</Key>
        <Key className="h-[5px] w-3.5 rounded-sm bg-warn">超出团队要求的部分</Key>
        <Key className="h-3 w-[2px] rounded-sm bg-ink-2">团队要求时间</Key>
        <Key className="h-[3px] w-3.5 rounded-sm bg-ink-3 opacity-50">
          改过的旧承诺
        </Key>
        <Key className="h-3 w-px bg-series-b opacity-60">当前时间</Key>
      </div>
    </div>
  );
}

function Key({
  className,
  children,
}: {
  className: string;
  children: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <i className={`shrink-0 ${className}`} />
      {children}
    </span>
  );
}
