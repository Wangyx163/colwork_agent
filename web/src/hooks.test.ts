import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

/** A hook declared after an early return runs on some renders and not others.
 *
 *  React calls that error #310 and reports it minified, from inside the
 *  runtime, with a stack that names no file of ours. The page it breaks is the
 *  one that was working a moment earlier, so nothing points at the cause --
 *  which is what happened here: a `useState` added next to the code that used
 *  it, two early returns above it, and a blank workbench.
 *
 *  `eslint-plugin-react-hooks` is the usual guard. There is no eslint in this
 *  repository and adding one for a single rule is a large dependency for a
 *  small check, so the check reads the source instead. It relies on the file's
 *  own formatting: a statement at the top level of a component body sits at
 *  exactly two spaces, which prettier keeps true.
 */
function sourceFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else if (entry.name.endsWith(".tsx")) found.push(path);
  }
  return found;
}

const HERE = new URL(".", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

/** A hook call at the top level of a component body -- two spaces in. */
const HOOK = /^ {2}(?:const|let)\s*(?:\[[^\]]*\]|\w+)\s*=\s*use[A-Z]|^ {2}use[A-Z]\w*\(/;

/** A return belonging to the component body rather than to a callback inside
 *  it. Depth is read off the indentation, which prettier keeps honest: the
 *  body sits at two spaces, and a guard clause written `if (x)` on one line
 *  puts its `return` at four.
 *
 *  Matching every `return` at any depth was the second wrong version -- it
 *  read `return map;` inside a useMemo as an early return and flagged three
 *  hooks that were fine. A check that cries wolf gets the offending line
 *  deleted, not the offending code fixed.
 */
const BODY_RETURN = /^ {2}return[\s(<;]/;
const GUARDED_RETURN = /^ {4}return[\s(<;]/;
const GUARD_CLAUSE = /^ {2}(?:if|for|while)\s*\(/;

const TOP_LEVEL = /^(?:export |default |function |const |class |async )/;

test("no component declares a hook after an early return", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(HERE)) {
    const lines = readFileSync(file, "utf8").split("\n");
    let returned = false;
    let previousCode = "";
    lines.forEach((line, index) => {
      if (TOP_LEVEL.test(line)) {
        returned = false;
        previousCode = line;
        return;
      }
      if (
        BODY_RETURN.test(line) ||
        (GUARDED_RETURN.test(line) && GUARD_CLAUSE.test(previousCode))
      ) {
        returned = true;
      }
      if (line.trim()) previousCode = line;
      if (returned && HOOK.test(line)) {
        offenders.push(`${file}:${index + 1}: ${line.trim().slice(0, 60)}`);
      }
    });
  }

  assert.deepEqual(
    offenders,
    [],
    "these run on some renders and not others; move them above the early returns",
  );
});
