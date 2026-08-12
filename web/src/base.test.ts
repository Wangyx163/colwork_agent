import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

/** Reading the source rather than rendering it.
 *
 *  The bug this catches is not a wrong value from a function -- `pageUrl` was
 *  correct the day it was written. It is a link somewhere in the tree that
 *  never called it: absolute internal hrefs were right while one meeting sat
 *  at the root, and became silently wrong the moment meetings got a prefix,
 *  because the link left the meeting and the server answered
 *  `unknown_meeting`. Nothing failed; the page just stopped working.
 */
function sourceFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    // Tests excluded: this one quotes the very shapes it forbids, and a
    // guard that fails on its own examples teaches people to weaken it.
    else if (
      (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts")) &&
      !entry.name.endsWith(".test.ts")
    )
      found.push(path);
  }
  return found;
}

const HERE = new URL(".", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

/** The reads and links that belong to no meeting, so the ones that must not be
 *  prefixed. Exempted by path rather than by file: if one of those pages ever
 *  reaches for something else absolute, this still catches it.
 *
 *  `/observatory` reads across every meeting and is gated by an operator
 *  credential rather than by a meeting role, so prefixing it would ask one
 *  meeting for a reading of all of them. `/` is the meeting index.
 */
const ABOVE_EVERY_MEETING = new Set([
  "/api/meetings",
  "/api/observatory",
  "/observatory",
  "/",
]);

/** The path part alone: a query string and a template interpolation are both
 *  things that follow the path rather than change which path it is. */
function pathOf(raw: string): string {
  return raw.split("?")[0].split("$")[0];
}

test("no page links to another page with an absolute path", () => {
  // Meetings.tsx is the one page above every meeting: its links come from the
  // server already carrying the slug, and prefixing them would ask a meeting
  // for the list of meetings.
  const offenders: string[] = [];
  for (const file of sourceFiles(HERE)) {
    if (file.endsWith("Meetings.tsx")) continue;
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(/href="(\/[^"]*)"/g)) {
      if (ABOVE_EVERY_MEETING.has(pathOf(match[1]))) continue;
      offenders.push(`${file}: ${match[1]}`);
    }
  }

  assert.deepEqual(
    offenders,
    [],
    "these leave the meeting they were clicked in; wrap them in pageUrl()",
  );
});


test("every fetch goes through the api module", () => {
  // Same failure on the request side: a request sent without the prefix
  // reaches another meeting, or none, and comes back 404 or 401.
  const offenders: string[] = [];
  for (const file of sourceFiles(HERE)) {
    if (file.endsWith("api.ts")) continue;
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(/fetch\(\s*(["'`])(\/[^"'`]*)/g)) {
      if (ABOVE_EVERY_MEETING.has(pathOf(match[2]))) continue;
      offenders.push(`${file}: ${match[2]}`);
    }
  }

  assert.deepEqual(
    offenders,
    [],
    "these bypass apiUrl() and so bypass the meeting prefix",
  );
});
