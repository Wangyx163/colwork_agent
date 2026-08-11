from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = ROOT / "web" / "src"
SERVER = ROOT / "src" / "collab_agent" / "web.py"


def declared_routes() -> set[str]:
    """Every write route the server accepts, as a template path.

    Read out of the compiled patterns rather than a hand-kept list, because a
    hand-kept list is the thing that goes stale.
    """

    source = SERVER.read_text(encoding="utf-8")
    routes: set[str] = set()
    for pattern in re.findall(r'r"\^(/api/[^"]+)\$"', source):
        # A path parameter first, so what remains in brackets is only ever the
        # alternation of sibling verbs.
        template = pattern.replace("([^/]+)", "{id}")
        alternation = re.search(r"\(([^)]*\|[^)]*)\)", template)
        if alternation:
            for verb in alternation.group(1).split("|"):
                routes.add(
                    template[: alternation.start()]
                    + verb
                    + template[alternation.end() :]
                )
        else:
            routes.add(template)
    return routes


def wired_calls() -> set[str]:
    """Every /api/... path the built pages actually call."""

    called: set[str] = set()
    for path in WEB_SRC.rglob("*.ts*"):
        if path.name.endswith(".test.ts"):
            continue
        for raw in re.findall(r"[\"`](/api/[^\"`\s?]+)", path.read_text(encoding="utf-8")):
            # Template holes become the same placeholder the routes use.
            # A ${...} directly after a path segment with no slash before it
            # is a query string being appended, not a path parameter.
            normalized = re.sub(r"(?<=/)\$\{[^}]*\}", "{id}", raw)
            normalized = re.sub(r"\$\{[^}]*\}$", "", normalized)
            called.add(normalized.rstrip("/"))
    return called


#: Routes with no page behind them, each for a stated reason. Anything else
#: appearing here is a capability the backend has and nobody can use.
KNOWN_UNREACHABLE = {
    # Answered by the questionnaire's own flow, which declares rather than
    # replaces; replacing a draft is reachable from the Memory panel as
    # confirm/withdraw, and a third verb would only duplicate them.
    "/api/memories/{id}/replace",
}


class WriteRoutesAreReachableTests(unittest.TestCase):
    """A backend capability nobody can reach is the same as one that is missing.

    Eight of these went unreachable at once when the server-rendered page was
    replaced, and nothing failed -- the routes still worked, the tests still
    passed, and the abilities were simply gone from the product. This is the
    check that would have caught it, so it exists rather than a resolution to
    be more careful.
    """

    def test_every_write_route_has_a_caller(self) -> None:
        routes = declared_routes()
        called = wired_calls()

        self.assertTrue(routes, "no routes were parsed; the reader is broken")
        unreachable = {
            route
            for route in routes
            if route not in called and route not in KNOWN_UNREACHABLE
        }

        self.assertEqual(
            unreachable,
            set(),
            "these exist on the server and cannot be reached from any page",
        )

    def test_the_allowlist_does_not_outlive_its_reason(self) -> None:
        """An exemption for a route that is now wired is stale bookkeeping."""

        called = wired_calls()

        self.assertEqual(
            {route for route in KNOWN_UNREACHABLE if route in called},
            set(),
            "these are wired now and should leave the allowlist",
        )

    def test_no_page_calls_a_route_the_server_does_not_have(self) -> None:
        routes = declared_routes() | {
            # Read paths, which this check is not about.
            "/api/state",
            "/api/session",
            "/api/session/actors",
            "/api/observatory",
            # Above every meeting rather than inside one, so it is not in the
            # per-meeting route table the other side of this check reads.
            "/api/meetings",
        }
        called = wired_calls()

        self.assertEqual(
            {call for call in called if call.split("?")[0] not in routes},
            set(),
            "these would 404 the moment somebody clicked them",
        )


if __name__ == "__main__":
    unittest.main()
