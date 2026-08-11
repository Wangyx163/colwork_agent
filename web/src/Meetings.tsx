import { useEffect, useState } from "react";

interface Meeting {
  slug: string;
  title: string;
  manage_url: string;
  tasks_url: string;
}

/** The door onto a console serving several meetings.
 *
 *  Deliberately the only page that works without a token: a token is minted
 *  per meeting, so this is what somebody has before they have one. It shows
 *  which meetings exist and nothing from inside any of them -- no task, no
 *  status, no roster. Anything more would be a hole in the per-meeting
 *  authorization the rest of the console rests on.
 */
export default function MeetingsPage() {
  const [meetings, setMeetings] = useState<Meeting[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    // Not through apiUrl: this page is above every meeting, so prefixing it
    // with one would ask a meeting for the list of meetings.
    fetch("/api/meetings")
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.message || "读取失败");
        return body.meetings as Meeting[];
      })
      .then((rows) => {
        if (!cancelled) setMeetings(rows);
      })
      .catch((problem: Error) => {
        if (!cancelled) setError(problem.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="mx-auto max-w-2xl px-5 py-10">
      <h1 className="text-[1.15rem] font-semibold tracking-tight">会议</h1>
      <p className="mt-1.5 max-w-[60ch] text-[0.85rem] leading-relaxed text-ink-2">
        每场会有自己的地址、自己的参会名单、自己的登录。换一场会要重新选身份——
        一个会的令牌在另一个会里不作数。
      </p>

      {error ? (
        <p className="mt-6 rounded-md border border-rule-2 bg-raise px-4 py-3 text-[0.85rem] text-ink-2">
          {error}
        </p>
      ) : meetings === null ? (
        <p className="mt-6 text-[0.85rem] text-ink-3">读取中…</p>
      ) : meetings.length === 0 ? (
        <p className="mt-6 text-[0.85rem] text-ink-3">还没有会议。</p>
      ) : (
        <ul className="mt-6 grid gap-2">
          {meetings.map((meeting) => (
            <li
              key={meeting.slug}
              className="rounded-md border border-rule-2 bg-raise px-4 py-3"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-[0.92rem] font-semibold">
                  {meeting.title}
                </span>
                <span className="font-mono text-[0.7rem] text-ink-3">
                  /{meeting.slug}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <a
                  className="rounded border border-rule px-2.5 py-1 text-[0.79rem] hover:bg-ground"
                  href={meeting.tasks_url}
                >
                  我的任务
                </a>
                <a
                  className="rounded border border-rule px-2.5 py-1 text-[0.79rem] hover:bg-ground"
                  href={meeting.manage_url}
                >
                  会议工作台
                </a>
              </div>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
