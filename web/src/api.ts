import { basePath, tokenKey } from "./base";

/** The workbench issues a session token and stores it here; the React pages
 *  reuse it rather than minting an identity of their own, so the same
 *  coordinator check applies on the server. Keyed by meeting, because a token
 *  names an actor inside one episode and means nothing in another. */
export function authHeaders(): HeadersInit {
  const token = localStorage.getItem(tokenKey());
  return token ? { authorization: `Bearer ${token}` } : {};
}

/** Every request goes through here so the meeting prefix is applied in one
 *  place. Adding it at each call site is the version of this that works until
 *  somebody adds the twentieth fetch and forgets. */
export function apiUrl(path: string): string {
  return path.startsWith("/") ? `${basePath()}${path}` : path;
}

export async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(apiUrl(url), { headers: authHeaders() });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || body.error || "读取失败");
  return body as T;
}

export async function postJson<T>(
  url: string,
  payload: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(apiUrl(url), {
    method: "POST",
    headers: { ...authHeaders(), "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || body.error || "操作失败");
  return body as T;
}

/** A stable id for anything the server treats as one submission. Reusing it on
 *  a retry is what makes a repeated click land once. */
export function messageId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}`;
}
