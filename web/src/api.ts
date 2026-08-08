/** The workbench issues a session token and stores it here; the React pages
 *  reuse it rather than minting an identity of their own, so the same
 *  coordinator check applies on the server. */
export function authHeaders(): HeadersInit {
  const token = localStorage.getItem("collabSessionToken");
  return token ? { authorization: `Bearer ${token}` } : {};
}

export async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: authHeaders() });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || body.error || "读取失败");
  return body as T;
}

export async function postJson<T>(
  url: string,
  payload: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(url, {
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
