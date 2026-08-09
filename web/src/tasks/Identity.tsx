import { useEffect, useState } from "react";
import { getJson, postJson } from "../api";

export interface Actor {
  actor_id: string;
  display_name: string;
  roles: string[];
}

const TOKEN_KEY = "collabSessionToken";
const ACTOR_KEY = "collabSessionActor";

export function storedActor(): string {
  return localStorage.getItem(ACTOR_KEY) || "";
}

export function signOut() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ACTOR_KEY);
}

/** Choosing who you are.
 *
 *  This is a demo affordance, not authentication: the server mints a session
 *  for whoever is asked for. It says so on the screen, because a picker that
 *  looks like a login is a claim the system does not make. */
export function IdentityGate({ onReady }: { onReady: () => void }) {
  const [actors, setActors] = useState<Actor[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    getJson<{ actors: Actor[] }>("/api/session/actors")
      .then((body) => setActors(body.actors))
      .catch((problem: Error) => setError(problem.message));
  }, []);

  const choose = async (actor: Actor) => {
    setBusy(actor.actor_id);
    try {
      const body = await postJson<{ token: string }>("/api/session", {
        actor_id: actor.actor_id,
      });
      localStorage.setItem(TOKEN_KEY, body.token);
      localStorage.setItem(ACTOR_KEY, actor.display_name);
      onReady();
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <main className="mx-auto max-w-lg px-4 py-16">
      <h1 className="text-xl font-bold tracking-tight">你是谁？</h1>
      <p className="mt-2 text-[0.86rem] text-ink-2">
        选一个参会者身份进入工作台。这里是演示用的身份切换，不是登录——
        权限判断在服务端按角色做，前端只是替你要一个会话。
      </p>
      {error ? (
        <p className="mt-4 rounded border border-bad bg-bad-wash px-3 py-2 text-[0.85rem]">
          {error}
        </p>
      ) : null}
      <div className="mt-6 grid gap-2">
        {(actors || []).map((actor) => (
          <button
            key={actor.actor_id}
            onClick={() => void choose(actor)}
            disabled={Boolean(busy)}
            className="flex items-center justify-between rounded-md border border-rule bg-raise px-4 py-3 text-left hover:border-accent hover:bg-accent-wash focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
          >
            <span className="font-semibold">{actor.display_name}</span>
            <span className="font-mono text-[0.7rem] text-ink-3">
              {actor.roles.includes("COORDINATOR") ? "会议负责人" : "参会者"}
            </span>
          </button>
        ))}
        {actors && !actors.length ? (
          <p className="text-[0.85rem] text-ink-3">这场会议还没有参会者。</p>
        ) : null}
        {!actors && !error ? (
          <p className="text-[0.85rem] text-ink-3">正在读取参会名单…</p>
        ) : null}
      </div>
    </main>
  );
}
