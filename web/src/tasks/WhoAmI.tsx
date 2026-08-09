import { useEffect, useRef, useState } from "react";
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

export async function signIn(actor: Actor): Promise<void> {
  const body = await postJson<{ token: string }>("/api/session", {
    actor_id: actor.actor_id,
  });
  localStorage.setItem(TOKEN_KEY, body.token);
  localStorage.setItem(ACTOR_KEY, actor.display_name);
}

/** Switching who you are, in place.
 *
 *  Walking out to a picker and back is three steps for something done
 *  constantly while showing this system: the whole point of the demo is that
 *  the same meeting looks different depending on who is looking. So the
 *  switch stays in the header, and the links beside it are only the surfaces
 *  the chosen identity is actually allowed to open -- a coordinator gets the
 *  console, everybody else gets their own work and nothing that would 403. */
export function WhoAmI({
  name,
  coordinator,
  onSwitched,
}: {
  name: string;
  coordinator: boolean;
  onSwitched: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [actors, setActors] = useState<Actor[]>([]);
  const [busy, setBusy] = useState("");
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || actors.length) return;
    getJson<{ actors: Actor[] }>("/api/session/actors")
      .then((body) => setActors(body.actors))
      .catch(() => setActors([]));
  }, [open, actors.length]);

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const pick = async (actor: Actor) => {
    setBusy(actor.actor_id);
    try {
      await signIn(actor);
      setOpen(false);
      onSwitched();
    } finally {
      setBusy("");
    }
  };

  return (
    <div ref={box} className="relative">
      <button
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded border border-rule px-2.5 py-1 text-[0.8rem] hover:bg-sunk focus-visible:outline-2 focus-visible:outline-accent"
      >
        <span className="font-semibold">{name || "选择身份"}</span>
        <span className="font-mono text-[0.68rem] text-ink-3">
          {coordinator ? "负责人" : "参会者"}
        </span>
        <span className="text-ink-3">{open ? "▴" : "▾"}</span>
      </button>

      {open ? (
        <div className="absolute top-full right-0 z-20 mt-2 w-56 overflow-hidden rounded-md border border-rule bg-raise shadow-lg">
          {actors.map((actor) => {
            const current = actor.display_name === name;
            return (
              <button
                key={actor.actor_id}
                onClick={() => void pick(actor)}
                disabled={Boolean(busy) || current}
                aria-current={current}
                className={`flex w-full items-center justify-between border-b border-rule-2 px-3 py-2 text-left last:border-b-0 hover:bg-sunk disabled:cursor-default ${
                  current ? "bg-accent-wash" : ""
                }`}
              >
                <span className="text-[0.84rem]">{actor.display_name}</span>
                <span className="font-mono text-[0.68rem] text-ink-3">
                  {actor.roles.includes("COORDINATOR") ? "负责人" : "参会者"}
                </span>
              </button>
            );
          })}
          {!actors.length ? (
            <p className="px-3 py-3 text-[0.8rem] text-ink-3">正在读取…</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** The first visit, when there is no session yet. */
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
      await signIn(actor);
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
        选一个参会者身份进入工作台。这是演示用的身份切换，不是登录——权限判断在服务端按角色做。
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
              {actor.roles.includes("COORDINATOR")
                ? "会议负责人 · 能看全部"
                : "参会者 · 只看自己的任务"}
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
