import { pagePath } from "./base";
import ManagePage from "./Manage";
import MeetingsPage from "./Meetings";
import ObservatoryPage from "./Observatory";
import TasksPage from "./Tasks";

/** One bundle backs every page, so the route is read off the path the server
 *  handed us rather than kept in a router: every page route serves the same
 *  index.html, and the first render already knows which one it is. */
const PAGES = [
  { at: "/manage", title: "会议工作台", render: () => <ManagePage /> },
  { at: "/observatory", title: "Agent Observatory", render: () => <ObservatoryPage /> },
  // The old diagnostics page showed gate results and the agent trace; the
  // Observatory shows both and more, so the path survives as a second door
  // into it rather than as a page to keep in step with it.
  { at: "/diagnostics", title: "Agent Observatory", render: () => <ObservatoryPage /> },
  { at: "/tasks", title: "我的任务", render: () => <TasksPage /> },
] as const;

/** Only reachable when several meetings share a port: with one meeting the
 *  server sends the bare root straight to that meeting's task list, so this
 *  page never renders there. */
const INDEX_PAGE = {
  at: "/",
  title: "会议",
  render: () => <MeetingsPage />,
} as const;

/** The bare root lands on the task list: it is the only page every
 *  participant is allowed to open. */
const DEFAULT_PAGE = PAGES[PAGES.length - 1];

export default function App() {
  // The meeting prefix is stripped first: with several meetings on one port
  // the path is `/wangyuxiang01/manage`, and matching that against "/manage"
  // silently lands every page on the task list.
  const path = pagePath();
  const page =
    path === "/"
      ? INDEX_PAGE
      : (PAGES.find((entry) => path.startsWith(entry.at)) ?? DEFAULT_PAGE);
  document.title = page.title;
  return page.render();
}
