import { describe, type Row, type Strip } from "./schedule";

const GRID = "grid-cols-[5.5rem_1fr] sm:grid-cols-[10rem_1fr]";

/** The schedule strip.
 *
 *  The bar is the window the team allows: it opens at now and closes on the
 *  day the team needs the work. What follows it, when a person has promised a
 *  later date than that, is drawn rather than filled -- time nobody agreed to
 *  should not look as solid as time somebody did -- and carries the overrun in
 *  words at its end, because a reader should not have to measure a dashed
 *  segment against a date axis to learn it is two days.
 *
 *  Names ride inside their own bars. A separate column of people would align
 *  neatly and say nothing about which row it belonged to; putting the label in
 *  the bar costs no extra height, which is the whole reason the rows are one
 *  line tall. */
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
  const now = strip.now ?? 0;

  return (
    <div className="mb-4 overflow-hidden rounded-md border border-rule-2">
      {/* Axis. The tick that would sit under the now-mark is dropped: the
          mark is labelled itself, and two labels in one place read as one
          smudged label. */}
      <div className={`grid ${GRID} gap-2 border-b border-rule-2 bg-sunk px-3 pt-1 pb-0.5 sm:gap-3`}>
        <span />
        <span className="relative h-[1.1rem]">
          {strip.ticks
            .filter((tick) => !tick.today)
            .map((tick) => (
              <span
                key={tick.label}
                className="absolute top-0 -translate-x-1/2 font-mono text-[0.64rem] text-ink-3"
                style={{ left: `${tick.at * 100}%` }}
              >
                {tick.label}
              </span>
            ))}
          <span
            className="absolute top-0 -translate-x-1/2 rounded-sm bg-series-b px-1 font-mono text-[0.64rem] text-white"
            style={{ left: `${now * 100}%` }}
          >
            今天
          </span>
        </span>
      </div>

      {/* The rows, with the now-mark laid over just them: it belongs against
          the bars, not through the axis labels or the border below. */}
      <div className="relative">
        {strip.rows.map((row) => (
          <StripRow
            key={row.task.action_item_id}
            row={row}
            selected={selected === row.task.action_item_id}
            onPick={onPick}
          />
        ))}
        <div
          aria-hidden
          className={`pointer-events-none absolute inset-0 grid ${GRID} gap-2 px-3 sm:gap-3`}
        >
          <span />
          <span className="relative">
            <span
              className="absolute top-0 bottom-0 w-px bg-series-b opacity-45"
              style={{ left: `${now * 100}%` }}
            />
          </span>
        </div>
      </div>
    </div>
  );
}

/** Below this fraction of the window a bar is too short to hold a name, so
 *  the name goes after it instead of being clipped inside it. */
const NAME_FITS = 0.22;

function StripRow({
  row,
  selected,
  onPick,
}: {
  row: Row;
  selected: boolean;
  onPick: (actionItemId: string) => void;
}) {
  // The bar is what this person committed to; the cap is what the team needs.
  // Reading the two against each other is the whole point of the row: the bar
  // stopping short of the cap is slack, and running past it is the conflict.
  // Before a task is accepted there is no promise, so the bar falls back to
  // the requirement -- there is nothing yet to compare it against.
  const cap = row.required;
  const commitment = row.promised ?? cap ?? row.start;
  const solidEnd = cap === null ? commitment : Math.min(commitment, cap);
  const end = Math.max(commitment, cap ?? commitment);
  const width = Math.max(0, solidEnd - row.start);
  const inside = width >= NAME_FITS;

  return (
    <button
      onClick={() => onPick(row.task.action_item_id)}
      aria-current={selected}
      title={describe(row.task)}
      className={`grid w-full ${GRID} items-center gap-2 border-b border-rule-2 px-3 py-[3px] text-left last:border-b-0 hover:bg-sunk focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent sm:gap-3 ${
        selected ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent" : ""
      }`}
    >
      <span className="truncate text-[0.78rem]">{row.task.title}</span>

      <span className="relative h-[1.35rem]">
        <span className="absolute top-1/2 right-0 left-0 h-px -translate-y-1/2 bg-rule-2" />

        {/* Where the promise used to sit before it was moved.
            A mark, not a segment: "the date used to be here" is a point, and
            drawing it as a range made it vanish whenever the old promise
            happened to land on the team's own date -- which is precisely the
            common case, since people first agree to the date they were asked
            for and move it afterwards. Sits above the bar so it never hides
            under the team cap. */}
        {row.ghosts.map((ghost, index) => (
          <span
            key={index}
            title="改期前的承诺"
            className="absolute top-0 h-[5px] w-[2px] -translate-x-px rounded-full bg-ink-3 opacity-55"
            style={{ left: `${ghost * 100}%` }}
          />
        ))}

        {/* Finishing ahead of the deadline is worth seeing, so the gap the bar
            leaves before the cap is drawn rather than left as blank track. */}
        {cap !== null && commitment < cap ? (
          <span
            className="absolute top-1/2 h-[3px] -translate-y-1/2 rounded-full bg-ok/40"
            style={{
              left: `${commitment * 100}%`,
              width: `${(cap - commitment) * 100}%`,
            }}
            title="比团队要求早"
          />
        ) : null}

        {/* The window the team allows. */}
        <span
          className={`absolute top-1/2 flex h-[13px] -translate-y-1/2 items-center overflow-hidden rounded-[3px] ${
            row.done
              ? "bg-ok/85"
              : row.overdue
                ? "bg-bad/85"
                : "bg-accent/85"
          }`}
          style={{ left: `${row.start * 100}%`, width: `${width * 100}%` }}
        >
          {inside ? (
            <span className="truncate px-1.5 text-[0.66rem] leading-none font-medium text-white">
              {row.owner}
            </span>
          ) : null}
        </span>

        {/* The day the team needs it: the bar's closing edge, drawn as a cap
            so it survives being right next to the overrun. */}
        {row.required !== null ? (
          <span
            className="absolute top-1/2 h-[17px] w-[2px] -translate-x-px -translate-y-1/2 rounded-full bg-ink"
            style={{ left: `${row.required * 100}%` }}
          />
        ) : null}

        {/* Time promised beyond that. */}
        {row.late ? (
          <span
            className="absolute top-1/2 h-[3px] -translate-y-1/2 text-warn"
            style={{
              left: `${solidEnd * 100}%`,
              width: `${Math.max(0, end - solidEnd) * 100}%`,
              background:
                "repeating-linear-gradient(90deg,currentColor 0 3px,transparent 3px 6px)",
            }}
          />
        ) : null}

        {/* One trailing label rather than two competing for the same spot:
            how much the overrun is, and the name when the bar was too short
            to hold it. */}
        {row.late || !inside ? (
          <span
            className="absolute top-1/2 flex -translate-y-1/2 gap-1.5 pl-1.5 font-mono text-[0.64rem] whitespace-nowrap"
            style={{ left: `${end * 100}%` }}
          >
            {row.late ? (
              <b className="font-medium text-warn">晚 {row.lateDays} 天</b>
            ) : null}
            {!inside ? <span className="text-ink-3">{row.owner}</span> : null}
          </span>
        ) : null}
      </span>
    </button>
  );
}
