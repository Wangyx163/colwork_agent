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
  /** 0–1 positions across the shared window. */
  start: number;
  promised: number | null;
  required: number | null;
  /** Superseded promises, oldest first: where this person used to stand. */
  ghosts: number[];
  /** The promise runs past what the team needs -- the bar grows a warn tail. */
  late: boolean;
  /** Days late, positive only. Rounded to whole days like the deadlines are. */
  lateDays: number;
  done: boolean;
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
 *  The window is fitted to the data but never narrower than `minDays`, so a
 *  meeting whose deadlines all fall on one afternoon does not blow up into a
 *  strip where an hour reads as a week. */
export function buildStrip(
  tasks: Task[],
  nowIso: string | null | undefined,
  minDays = 7,
): Strip {
  const instants: number[] = [];
  const prepared = tasks.map((task) => {
    const start =
      ms(task.activity?.[task.activity.length - 1]?.sim_time) ??
      ms(task.deadline_sim_time) ??
      Date.now();
    const promised = ms(task.promised_by_sim_time);
    const required = ms(task.team_required_by_sim_time);
    const ghosts = supersededPromises(task);
    instants.push(start, ...ghosts);
    if (promised !== null) instants.push(promised);
    if (required !== null) instants.push(required);
    return { task, start, promised, required, ghosts };
  });

  const now = ms(nowIso);
  if (now !== null) instants.push(now);
  if (!instants.length) return { rows: [], ticks: [], now: null };

  let low = Math.min(...instants);
  let high = Math.max(...instants);
  // Pad to whole days so the ticks land on midnights a reader recognises.
  low = Math.floor(low / DAY) * DAY;
  high = Math.ceil(high / DAY) * DAY;
  const shortfall = minDays * DAY - (high - low);
  if (shortfall > 0) {
    low -= Math.floor(shortfall / 2);
    high += Math.ceil(shortfall / 2);
  }
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
    ({ task, start, promised, required, ghosts }) => {
      const late =
        promised !== null && required !== null && promised > required;
      return {
        task,
        start: at(start),
        promised: promised === null ? null : at(promised),
        required: required === null ? null : at(required),
        ghosts: ghosts.map(at),
        late,
        lateDays: late ? Math.round((promised! - required!) / DAY) : 0,
        done: DONE.has(task.status),
      };
    },
  );

  return { rows, ticks, now: now === null ? null : at(now) };
}

export function formatDay(value: string | null | undefined): string {
  const parsed = ms(value);
  if (parsed === null) return "——";
  const instant = new Date(parsed);
  return `${instant.getMonth() + 1}/${String(instant.getDate()).padStart(2, "0")}`;
}
