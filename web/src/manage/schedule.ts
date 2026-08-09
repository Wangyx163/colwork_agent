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
  /** Whole days past the team date, when overdue. The strip does not spend
   *  width on time that has gone, so lateness is stated rather than measured. */
  overdueDays: number;
  /** A date that falls before the window and has been pinned to its left edge,
   *  so a mark drawn there is not read as "due today". */
  clamped: boolean;
}

export interface Strip {
  rows: Row[];
  /** Day ticks across the window, as label + 0–1 position. */
  ticks: { label: string; at: number; today: boolean }[];
  now: number | null;
}

/** Midnight *where the reader is*.
 *
 *  Flooring the epoch to a multiple of a day gives midnight UTC, which is some
 *  other hour locally -- so the axis would step on the wrong boundary while
 *  its labels named local dates, and the mark for now would land a few hours
 *  off the tick it belongs to. */
function startOfDay(value: number): number {
  const instant = new Date(value);
  instant.setHours(0, 0, 0, 0);
  return instant.getTime();
}

function addDays(value: number, days: number): number {
  const instant = new Date(value);
  instant.setDate(instant.getDate() + days);
  return instant.getTime();
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
 *  The window opens at *now* and never reaches back. Time that has gone is not
 *  something anyone can plan against, and spending width on it squeezes the
 *  part that can be acted on -- three days of history push every remaining bar
 *  into the right half of the strip. A deadline that has already passed is
 *  pinned to the left edge and says how late it is in words instead, which is
 *  what a reader wanted from that distance anyway. */
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

  // Today's midnight, always. The window never opens earlier, so the axis
  // starts where planning starts.
  const low = startOfDay(now);
  const latest = Math.max(...instants, low);
  let high = startOfDay(latest);
  if (high <= latest) high = addDays(high, 1);
  const floor = addDays(low, minDays);
  if (high < floor) high = floor;
  const span = high - low || DAY;
  // Anything behind the window sits on its edge rather than off the strip.
  const at = (value: number) =>
    Math.min(1, Math.max(0, (value - low) / span));

  const today = startOfDay(now);
  const ticks: { label: string; at: number; today: boolean }[] = [];
  const days = Math.round((high - low) / DAY);
  // Thin the labels rather than let them collide: past roughly a fortnight
  // every other midnight is enough to read the axis by.
  const step = days <= 14 ? 1 : Math.ceil(days / 12);
  for (let day = 0; day <= days; day += step) {
    const midnight = addDays(low, day);
    const instant = new Date(midnight);
    ticks.push({
      label: `${instant.getMonth() + 1}/${String(instant.getDate()).padStart(2, "0")}`,
      at: at(midnight),
      today: midnight === today,
    });
  }

  const rows: Row[] = prepared.map(
    ({ task, promised, required, ghosts }) => {
      const late =
        promised !== null && required !== null && promised > required;
      const done = DONE.has(task.status);
      const overdue = !done && required !== null && required < now;
      const earliest = Math.min(promised ?? now, required ?? now);
      return {
        task,
        owner:
          task.assigned_owner_display_name ||
          task.owner_display_name ||
          "未指派",
        // Every bar starts at now: it is the remaining runway that matters.
        start: at(Math.max(now, Math.min(now, earliest))),
        promised: promised === null ? null : at(promised),
        required: required === null ? null : at(required),
        ghosts: ghosts.map(at),
        late,
        lateDays: late ? Math.round((promised! - required!) / DAY) : 0,
        done,
        overdue,
        overdueDays: overdue
          ? Math.max(1, Math.round((startOfDay(now) - startOfDay(required!)) / DAY))
          : 0,
        clamped: earliest < low,
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
