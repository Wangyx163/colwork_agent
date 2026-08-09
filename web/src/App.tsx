import ManagePage from "./Manage";
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

/** The bare root lands on the task list: it is the only page every
 *  participant is allowed to open. */
const DEFAULT_PAGE = PAGES[PAGES.length - 1];

export default function App() {
  const path = window.location.pathname;
  const page =
    PAGES.find((entry) => path.startsWith(entry.at)) ?? DEFAULT_PAGE;
  document.title = page.title;
  return page.render();
}
