import assert from "node:assert/strict";
import { test } from "node:test";

// Extensions are explicit here because Node resolves this file directly,
// without the bundler that rewrites them everywhere else.
import type { Task } from "../manage-types.ts";
import { buildStrip, supersededPromises } from "./schedule.ts";

/* Run with `npm test` in web/. Node executes TypeScript directly, so this
   needs no test runner and no dependency, and CI stays a pure Python job.
   The geometry is the one piece of this page that is arithmetic rather than
   markup -- reasoning about it in a review is exactly how a bar ends up
   drawn a day wide or a marker ends up at zero width. */

const NOW = "2026-08-09T09:00:00+10:00";

function task(overrides: Partial<Task> = {}): Task {
  return {
    action_item_id: "ai_1",
    title: "任务",
    status: "TRACKING",
    required: 1,
    owner_actor_id: "a1",
    owner_display_name: "甲",
    assigned_owner_display_name: null,
    deliverable_key: "d",
    deadline_sim_time: null,
    team_required_by_sim_time: "2026-08-12T17:00:00+10:00",
    promised_by_sim_time: "2026-08-12T17:00:00+10:00",
    schedule_status: "ON_TIME",
    schedule_risk_reason: "",
    definition_version: 1,
    version: 1,
    source_span: "",
    activity: [],
    assignments: [],
    current_assignments: [],
    assistance_requests: [],
    active_assistance: null,
    collaborators: [],
    latest_version: null,
    current_version: null,
    accepted_task_result: null,
    latest_progress: null,
    ...overrides,
  };
}

function commitment(promised: string, status: string) {
  return {
    kind: "COMMITMENT" as const,
    title: "承诺",
    detail: `承诺截止：${promised}`,
    status,
    actor: "甲",
    actor_id: "a1",
    sim_time: "2026-08-09T10:00:00+10:00",
    promised_deadline_sim_time: promised,
  };
}

test("bars begin at the now-mark, and the axis opens on today", () => {
  const strip = buildStrip([task()], NOW);

  assert.equal(
    strip.rows[0].start,
    strip.now,
    "the now-mark is the origin every bar is read from",
  );
  assert.equal(strip.ticks[0].today, true, "the first midnight is today's");
  assert.equal(strip.ticks[0].at, 0);
});

test("day boundaries are the reader's midnights, not UTC's", () => {
  /* Flooring the epoch to a multiple of a day gives midnight UTC, which is
     some other hour locally: the axis would then step on one boundary while
     its labels named another, and the now-mark would sit hours away from the
     tick it belongs to. That is what put two labels on top of each other. */
  const strip = buildStrip([task()], NOW);
  const first = strip.ticks[0];
  const second = strip.ticks[1];
  const midnightGap = second.at - first.at;

  assert.ok(
    Math.abs(midnightGap * 7 - 1) < 1e-9,
    `ticks should be exactly one day apart across a 7-day window, got ${midnightGap}`,
  );
  assert.ok(
    strip.now! > 0 && strip.now! < midnightGap,
    "9am sits inside the first day, not on its edge and not past it",
  );
});

test("it reaches back only far enough to keep an overdue date visible", () => {
  const strip = buildStrip(
    [task({ team_required_by_sim_time: "2026-08-06T17:00:00+10:00" })],
    NOW,
  );

  assert.ok(strip.now! > 0, "now is no longer the left edge");
  assert.ok(
    strip.rows[0].required! < strip.now!,
    "a date that has passed sits behind the now-mark",
  );
  assert.equal(strip.rows[0].overdue, true);
});

test("a single afternoon of deadlines still spans a readable window", () => {
  const strip = buildStrip([task()], NOW, 7);
  const days = strip.ticks.length - 1;

  assert.ok(days >= 7, `expected at least 7 days of axis, got ${days}`);
});

test("a promise past the team date is late by whole days", () => {
  const strip = buildStrip(
    [task({ promised_by_sim_time: "2026-08-14T17:00:00+10:00" })],
    NOW,
  );
  const row = strip.rows[0];

  assert.equal(row.late, true);
  assert.equal(row.lateDays, 2);
  assert.ok(row.promised! > row.required!);
});

test("a promise ahead of the team date is slack, not lateness", () => {
  /* Real data hit this before any invented case did: somebody promised a day
     earlier than they were asked for. A strip that only ever draws the team's
     window renders that person identically to one scraping in on the day. */
  const row = buildStrip(
    [task({ promised_by_sim_time: "2026-08-11T17:00:00+10:00" })],
    NOW,
  ).rows[0];

  assert.equal(row.late, false);
  assert.equal(row.lateDays, 0);
  assert.ok(
    row.promised! < row.required!,
    "the promise has a position of its own, ahead of the requirement",
  );
});

test("a promise on the team date is not late", () => {
  const row = buildStrip([task()], NOW).rows[0];

  assert.equal(row.late, false);
  assert.equal(row.lateDays, 0);
});

test("superseded promises are read as dates, never parsed out of the prose", () => {
  const withHistory = task({
    promised_by_sim_time: "2026-08-14T17:00:00+10:00",
    activity: [
      commitment("2026-08-14T17:00:00+10:00", "ACTIVE"),
      commitment("2026-08-12T17:00:00+10:00", "SUPERSEDED"),
    ],
  });

  assert.equal(supersededPromises(withHistory).length, 1);
  assert.equal(buildStrip([withHistory], NOW).rows[0].ghosts.length, 1);
});

test("a superseded promise landing on the team date still has a position", () => {
  /* The common case: people accept the date they were asked for and move it
     afterwards, so the old promise coincides with the team tick. Drawing it
     as a span made it zero-wide and invisible exactly then. */
  const strip = buildStrip(
    [
      task({
        promised_by_sim_time: "2026-08-14T17:00:00+10:00",
        activity: [commitment("2026-08-12T17:00:00+10:00", "SUPERSEDED")],
      }),
    ],
    NOW,
  );
  const row = strip.rows[0];

  assert.equal(row.ghosts.length, 1);
  assert.equal(row.ghosts[0], row.required, "it sits on the team date");
  assert.ok(row.ghosts[0] > 0 && row.ghosts[0] < 1);
});

test("every task shares one window, so bars are comparable", () => {
  const strip = buildStrip(
    [
      task({ action_item_id: "a", team_required_by_sim_time: "2026-08-11T17:00:00+10:00", promised_by_sim_time: "2026-08-11T17:00:00+10:00" }),
      task({ action_item_id: "b", team_required_by_sim_time: "2026-08-16T17:00:00+10:00", promised_by_sim_time: "2026-08-16T17:00:00+10:00" }),
    ],
    NOW,
  );

  assert.ok(
    strip.rows[0].required! < strip.rows[1].required!,
    "the earlier deadline sits to the left of the later one",
  );
  for (const row of strip.rows) {
    assert.ok(row.required! >= 0 && row.required! <= 1, "inside the window");
  }
});

test("a finished task is marked done rather than overdue", () => {
  const row = buildStrip(
    [
      task({
        status: "ARCHIVED",
        team_required_by_sim_time: "2026-08-06T17:00:00+10:00",
      }),
    ],
    NOW,
  ).rows[0];

  assert.equal(row.done, true);
  assert.equal(row.overdue, false, "finished late is history, not a problem");
});

test("a task with no dates at all does not produce NaN geometry", () => {
  const strip = buildStrip(
    [task({ team_required_by_sim_time: null, promised_by_sim_time: null })],
    NOW,
  );
  const row = strip.rows[0];

  assert.equal(row.required, null);
  assert.equal(row.promised, null);
  assert.ok(Number.isFinite(row.start));
});

test("no tasks means no strip rather than a divide by zero", () => {
  const strip = buildStrip([], NOW);

  assert.deepEqual(strip.rows, []);
  assert.deepEqual(strip.ticks, []);
});
