/** Which meeting this page is showing, read off the URL.
 *
 *  One process can serve several meetings, each under `/<slug>/`. The bundle
 *  is the same for all of them, so the page cannot be told at build time which
 *  one it is -- it has to look at where it was opened.
 *
 *  Everything downstream depends on getting this right in two places, and they
 *  fail differently. A request sent without the prefix reaches another meeting
 *  and is refused, which is loud. A session token stored without the prefix is
 *  silently shared between meetings, and the second meeting opened in the same
 *  browser would try to act as an actor who does not exist in it.
 */

/** Paths the console owns itself. A first segment that is one of these is a
 *  page, not a meeting -- which is what the single-meeting layout looks like. */
const PAGE_SEGMENTS = new Set([
  "manage",
  "tasks",
  "observatory",
  "diagnostics",
  "api",
  "console",
]);

export function meetingSlug(): string {
  const first = window.location.pathname.split("/")[1] ?? "";
  return first && !PAGE_SEGMENTS.has(first) ? first : "";
}

/** Prefix for every request this page makes. Empty when one meeting is served
 *  at the root, which is still the common case. */
export function basePath(): string {
  const slug = meetingSlug();
  return slug ? `/${slug}` : "";
}

/** The path within the meeting, with the slug taken off -- what the router
 *  matches against. */
export function pagePath(): string {
  const slug = meetingSlug();
  const path = window.location.pathname;
  return slug ? path.slice(slug.length + 1) || "/" : path;
}

/** Session storage is per meeting for the same reason the server mints tokens
 *  per meeting: an actor id only means anything inside one episode. */
export function tokenKey(): string {
  const slug = meetingSlug();
  return slug ? `collabSessionToken:${slug}` : "collabSessionToken";
}
