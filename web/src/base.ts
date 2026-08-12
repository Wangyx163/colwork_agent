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

/** An href to another page of *this* meeting.
 *
 *  Every internal link used to be written absolute -- `/tasks`, `/manage` --
 *  which was correct while one meeting sat at the root and silently wrong the
 *  moment meetings got a prefix: the link left the meeting entirely and the
 *  server answered `unknown_meeting`. Links to somewhere outside a meeting
 *  (the index) and links a person submitted (attachments, references) are not
 *  this and must not go through it.
 */
export function pageUrl(path: string): string {
  return path.startsWith("/") ? `${basePath()}${path}` : path;
}

/** Session storage is per meeting for the same reason the server mints tokens
 *  per meeting: an actor id only means anything inside one episode. */
export function tokenKey(): string {
  const slug = meetingSlug();
  return slug ? `collabSessionToken:${slug}` : "collabSessionToken";
}

/** The Observatory's credential, which is not a meeting token.
 *
 *  Deliberately not keyed by meeting: the Observatory reads across all of
 *  them, so a per-meeting copy would be a credential that implies a scope it
 *  does not have. Held in localStorage rather than in the URL, because a token
 *  in a URL ends up in history, in a screenshot, and in whatever somebody
 *  pastes into chat.
 */
export const OPERATOR_TOKEN_KEY = "collabOperatorToken";

export function operatorToken(): string {
  return localStorage.getItem(OPERATOR_TOKEN_KEY) || "";
}

export function setOperatorToken(token: string): void {
  const trimmed = token.trim();
  if (trimmed) localStorage.setItem(OPERATOR_TOKEN_KEY, trimmed);
  else localStorage.removeItem(OPERATOR_TOKEN_KEY);
}
