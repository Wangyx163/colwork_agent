import type { Task } from "../manage-types";

const DAY = 86_400_000;

/** What one row of the schedule strip needs in order to draw itself.
 *
 *  The bar's *length* is the least interesting thing on it. What a coordinator
 *  reads is the relationship between three instants: when the person promised
 *  to be done, when the team needs it, and where a promise used to sit before
 *  it was revised. Those come from three different tables, so they are three
 *  separate fields here rather than one range. */
export interface Row {
  task: Task;
  owner: string;
  /** 0–1 positions across the shared window. */
  start: number;
  promised: number | null;
  required: number | null;
  /** Superseded promises, oldest first: where this person used to stand. */
  ghosts: number[];
  /** The promise runs past what the team needs. */
  late: boolean;
  /** Days late, positive only. Rounded to whole days like the deadlines are. */
  lateDays: number;
  done: boolean;
  /** Already past its team date without being finished. */
  overdue: boolean;
}

export interface Strip {
  rows: Row[];
  /** Day ticks across the window, as label + 0–1 position. */
  ticks: { label: string; at: number }[];
  now: number | null;
}

function ms(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/** Superseded promises for a task, oldest first.
 *
 *  Read off the structured field on the activity entry, never parsed out of
 *  its prose -- the sentence is free to change wording. */
export function supersededPromises(task: Task): number[] {
  return (task.activity || [])
    .filter(
      (entry) =>
        entry.kind === "COMMITMENT" &&
        entry.status === "SUPERSEDED" &&
        entry.promised_deadline_sim_time,
    )
    .map((entry) => ms(entry.promised_deadline_sim_time))
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b);
}

const DONE = new Set(["ACCEPTED", "AGGREGATED", "ARCHIVED"]);

/** Lay every task on one shared time window.
 *
 *  One window rather than per-row scaling: rows are compared against each
 *  other, and a bar that means a different number of days on every line would
 *  make that comparison a lie.
 *
 *  The window opens at *now* -- what is being read here is what remains, and
 *  the day a task was dispatched changes nothing a coordinator can act on. It
 *  only reaches back further when something is already overdue, because a
 *  deadline that has passed has to stay visible to be acted on; then the
 *  vertical mark sits inside the strip rather than on its left edge, and the
 *  distance behind it is exactly how late things are. */
export function buildStrip(
  tasks: Task[],
  nowIso: string | null | undefined,
  minDays = 7,
): Strip {
  const now = ms(nowIso) ?? Date.now();
  const instants: number[] = [now];
  const prepared = tasks.map((task) => {
    const promised = ms(task.promised_by_sim_time);
    const required = ms(task.team_required_by_sim_time);
    const ghosts = supersededPromises(task);
    instants.push(...ghosts);
    if (promised !== null) instants.push(promised);
    if (required !== null) instants.push(required);
    return { task, promised, required, ghosts };
  });

  if (!prepared.length) return { rows: [], ticks: [], now: null };

  let low = Math.min(...instants);
  let high = Math.max(...instants);
  low = Math.floor(low / DAY) * DAY;
  high = Math.ceil(high / DAY) * DAY;
  // Only ever widen to the right: pushing `low` earlier would move the
  // now-mark off the left edge for no reason, and that mark is the origin
  // every bar is read from.
  if (high - low < minDays * DAY) high = low + minDays * DAY;
  const span = high - low || DAY;
  const at = (value: number) => (value - low) / span;

  const ticks: { label: string; at: number }[] = [];
  const days = Math.round(span / DAY);
  // Thin the labels rather than let them collide: past roughly a fortnight
  // every other midnight is enough to read the axis by.
  const step = days <= 14 ? 1 : Math.ceil(days / 12);
  for (let day = 0; day <= days; day += step) {
    const instant = new Date(low + day * DAY);
    ticks.push({
      label: `${instant.getMonth() + 1}/${String(instant.getDate()).padStart(2, "0")}`,
      at: (day * DAY) / span,
    });
  }

  const rows: Row[] = prepared.map(
    ({ task, promised, required, ghosts }) => {
      const late =
        promised !== null && required !== null && promised > required;
      const done = DONE.has(task.status);
      return {
        task,
        owner:
          task.assigned_owner_display_name ||
          task.owner_display_name ||
          "未指派",
        // Every bar starts at now: it is the remaining runway that matters.
        start: at(Math.min(now, promised ?? now, required ?? now)),
        promised: promised === null ? null : at(promised),
        required: required === null ? null : at(required),
        ghosts: ghosts.map(at),
        late,
        lateDays: late ? Math.round((promised! - required!) / DAY) : 0,
        done,
        overdue: !done && required !== null && required < now,
      };
    },
  );

  return { rows, ticks, now: at(now) };
}

export function formatDay(value: string | null | undefined): string {
  const parsed = ms(value);
  if (parsed === null) return "——";
  const instant = new Date(parsed);
  return `${instant.getMonth() + 1}/${String(instant.getDate()).padStart(2, "0")}`;
}

/** The task's own description, for the tooltip.
 *
 *  Repeating the title there would say nothing the row does not already show. */
export function describe(task: Task): string {
  const meta = (task.proposal_metadata || {}) as Record<string, string>;
  const text =
    meta.deliverable || meta.work_requirements || task.deliverable_key || "";
  const criteria = meta.acceptance_criteria;
  return criteria ? `${text}\n验收标准：${criteria}` : text;
}
