import ManagePage from "./Manage";
import ObservatoryPage from "./Observatory";

/** One bundle backs both pages, so the route is read off the path the server
 *  handed us rather than kept in a router: every page route serves the same
 *  index.html, and the first render already knows which one it is. */
export default function App() {
  const manage = window.location.pathname.startsWith("/manage");
  document.title = manage ? "会议工作台" : "Agent Observatory";
  return manage ? <ManagePage /> : <ObservatoryPage />;
}
