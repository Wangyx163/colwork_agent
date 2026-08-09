// Stamp the built bundle with a fingerprint of the sources it came from.
//
// The build output is committed so a reviewer can clone and run the workbench
// with Python alone. The cost of that is drift: edit a source, forget to
// rebuild, commit, and the page silently keeps serving the previous UI while
// the diff says otherwise. That happened, and it costs the reader more than it
// costs the author -- they look at the old screen and conclude the change was
// never made.
//
// So the build records what it was built from, and a Python test recomputes
// the same fingerprint. CI stays Node-free: checking the stamp needs only a
// hash of files already in the repository.

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

const web = resolve(import.meta.dirname, "..");
const out = resolve(web, "../src/collab_agent/static/console");

/** Everything the bundle's contents depend on, excluding tests. */
const ROOTS = ["src", "index.html", "vite.config.ts", "tsconfig.app.json"];

function walk(path) {
  const found = [];
  const stat = statSync(path);
  if (stat.isFile()) return [path];
  for (const entry of readdirSync(path).sort()) {
    found.push(...walk(join(path, entry)));
  }
  return found;
}

const files = ROOTS.flatMap((root) => walk(resolve(web, root)))
  // A test file cannot change what the bundle renders, and letting it invalidate
  // the stamp would train everyone to ignore a failing stamp.
  .filter((file) => !file.endsWith(".test.ts"))
  .map((file) => relative(web, file).replaceAll("\\", "/"))
  // Sorted on the relative posix path, case-sensitively. Sorting the absolute
  // paths instead let this disagree with the Python side, whose Path ordering
  // is case-insensitive on Windows -- same files, same bytes, different order,
  // different hash.
  .sort();

const hash = createHash("sha256");
for (const file of files) {
  hash.update(file);
  hash.update("\0");
  hash.update(readFileSync(resolve(web, file)));
  hash.update("\0");
}

writeFileSync(
  join(out, "build-manifest.json"),
  JSON.stringify(
    { source_sha256: hash.digest("hex"), file_count: files.length },
    null,
    2,
  ) + "\n",
  "utf-8",
);
console.log(`stamped ${files.length} source files`);
