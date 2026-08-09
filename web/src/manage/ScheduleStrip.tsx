import { describe, type Strip } from "./schedule";

const GRID = "grid-cols-[6.5rem_1fr] sm:grid-cols-[9.5rem_1fr]";

/** The schedule strip.
 *
 *  Deliberately thin: 5px bars on a 20px track. A thicker bar would carry no
 *  more information -- the meaning is in where the marks sit relative to the
 *  team tick, not in how much ink each row uses.
 *
 *  There is no legend. Every mark is either labelled in place (the person's
 *  name rides the bar) or explained by the card below it, and a legend would
 *  only be a second thing to read before the first one makes sense. */
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
    <div className="relative mb-4 overflow-hidden rounded-md border border-rule-2">
      <div className={`grid ${GRID} gap-3 border-b border-rule-2 bg-sunk px-3 pt-1.5 pb-1`}>
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
        const end = row.promised ?? row.required ?? row.start;
        // The stretch beyond what the team asked for is drawn, not filled:
        // it is time that has not been agreed to, so it should not read as
        // solid as the part that has.
        const split = row.late && row.required !== null ? row.required : end;

        return (
          <button
            key={id}
            onClick={() => onPick(id)}
            aria-current={current}
            title={describe(row.task)}
            className={`grid w-full ${GRID} items-center gap-3 border-b border-rule-2 px-3 py-2 text-left last:border-b-0 hover:bg-sunk focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
              current ? "bg-accent-wash shadow-[inset_3px_0_0] shadow-accent" : ""
            }`}
          >
            <span className="truncate text-[0.79rem]">{row.task.title}</span>

            <span className="relative h-[26px]">
              <span className="absolute top-[17px] right-0 left-0 h-px bg-rule-2" />

              {/* The name rides its own bar, so a reader never has to match a
                  row back to a separate column of people. */}
              <span
                className="absolute top-0 max-w-[40%] truncate font-mono text-[0.66rem] text-ink-3"
                style={{ left: `${row.start * 100}%` }}
              >
                {row.owner}
              </span>

              {row.ghosts.map((ghost, index) => (
                <span
                  key={index}
                  aria-hidden
                  className="absolute top-[16px] h-[3px] rounded-sm text-ink-3 opacity-45"
                  style={{
                    left: `${Math.min(row.start, ghost) * 100}%`,
                    width: `${Math.abs(ghost - row.start) * 100}%`,
                    background:
                      "repeating-linear-gradient(90deg,currentColor 0 3px,transparent 3px 6px)",
                  }}
                />
              ))}

              <span
                className={`absolute top-[15px] h-[5px] rounded-sm ${
                  row.done ? "bg-ok" : row.overdue ? "bg-bad" : "bg-accent"
                }`}
                style={{
                  left: `${row.start * 100}%`,
                  width: `${Math.max(0, split - row.start) * 100}%`,
                }}
              />
              {row.late ? (
                <span
                  className="absolute top-[15px] h-[5px] rounded-r-sm text-warn"
                  style={{
                    left: `${split * 100}%`,
                    width: `${Math.max(0, end - split) * 100}%`,
                    background:
                      "repeating-linear-gradient(90deg,currentColor 0 4px,transparent 4px 7px)",
                  }}
                />
              ) : null}

              {row.required !== null ? (
                <span
                  className="absolute top-[11px] bottom-0 w-[2px] rounded-sm bg-ink-2"
                  style={{ left: `${row.required * 100}%` }}
                  title="团队要求交付"
                />
              ) : null}
            </span>
          </button>
        );
      })}

      {/* One mark for the whole strip rather than a repeat on every row: it is
          the same instant for everybody, and every bar is measured from it.
          Laid over the same grid so it stays aligned with the tracks without
          hard-coding the column width twice. */}
      {strip.now !== null ? (
        <div
          aria-hidden
          className={`pointer-events-none absolute inset-0 grid ${GRID} gap-3 px-3`}
        >
          <span />
          <span className="relative">
            <span
              className="absolute top-0 bottom-0 w-[2px] -translate-x-px bg-series-b opacity-70"
              style={{ left: `${strip.now * 100}%` }}
            />
          </span>
        </div>
      ) : null}
    </div>
  );
}
