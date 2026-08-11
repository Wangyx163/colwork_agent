from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database
from collab_agent.thread_local_store import ThreadLocalDatabase
from collab_agent.web import build_console_server, console_for


def extraction_for(quote: str) -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        # Distinct per meeting on purpose: the episode id is derived from
        # this, so two meetings sharing it would be one meeting. A real digest
        # rather than a formatted int -- the episode key takes the first 20
        # characters, and a zero-padded int is all zeros there.
        "input_sha256": hashlib.sha256(quote.encode()).hexdigest(),
        "action_items": [
            {
                "title": f"任务：{quote}",
                "deliverable": "一页说明",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:00",
                "source_quote": quote,
                "confidence": 0.9,
                "needs_confirmation": True,
                "uncertainties": [],
            }
        ],
    }


class MultiMeetingConsoleTests(unittest.TestCase):
    """Two meetings on one port, told apart by the first path segment.

    These go over a real socket rather than calling the handler's collaborators
    directly. That is the point: the mistakes this layer actually makes are
    mismatches between what one side sends and what the other reads, and those
    are invisible to any test that does not put a request on the wire.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        # A file plus ThreadLocalDatabase, matching how the CLI actually
        # serves: the server answers on its own thread, and sqlite3 refuses a
        # connection that crosses one. An in-memory database cannot be shared
        # this way at all, so using one here would test a shape that never
        # ships.
        path = root / "console.sqlite3"
        bootstrap = Database(path, allow_cross_thread=True)
        bootstrap.initialize()
        bootstrap.close()
        cls.db = ThreadLocalDatabase(
            lambda: Database(path, allow_cross_thread=True)
        )

        consoles = []
        cls.actors: dict[str, dict[str, str]] = {}
        for slug, coordinator, quote in (
            ("jiayi01", "甲一", "请把周报整理出来"),
            ("yier01", "乙二", "请把预算表核一遍"),
        ):
            extraction = root / f"{slug}.json"
            transcript = root / f"{slug}.txt"
            extraction.write_text(
                json.dumps(extraction_for(quote), ensure_ascii=False),
                encoding="utf-8",
            )
            transcript.write_text(f"主持人(00:01:00): {quote}\n", encoding="utf-8")
            service = load_meeting_service(
                cls.db,
                extraction_path=extraction,
                transcript_path=transcript,
                organization_name="多会议测试团队",
                coordinator_name=coordinator,
                participant_names=[coordinator, f"{coordinator}的同事"],
            )
            consoles.append(
                console_for(service, slug=slug, title=f"{coordinator}的会议")
            )
            cls.actors[slug] = {
                row["display_name"]: row["actor_id"]
                for row in cls.db.all(
                    "SELECT a.actor_id, a.display_name FROM actors a "
                    "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                    "WHERE ep.episode_id = ?",
                    (service.episode_id,),
                )
            }
            cls.actors[slug]["__coordinator__"] = service.aggregator_actor_id

        cls.server = build_console_server(consoles, host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls.db.close()
        cls.directory.cleanup()

    # ---- helpers -------------------------------------------------------

    def call(self, method: str, path: str, payload=None, token: str = ""):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        request.add_header("content-type", "application/json")
        if token:
            request.add_header("authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    return response.status, json.loads(raw or "{}")
                except json.JSONDecodeError:
                    return response.status, {"raw": raw[:200]}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(body or "{}")
            except json.JSONDecodeError:
                return error.code, {"raw": body[:200]}

    def token_for(self, slug: str, name: str = "__coordinator__") -> str:
        status, body = self.call(
            "POST", f"/{slug}/api/session", {"actor_id": self.actors[slug][name]}
        )
        self.assertEqual(status, 200, body)
        return body["token"]

    # ---- tests ---------------------------------------------------------

    def test_each_meeting_answers_with_its_own_tasks(self) -> None:
        for slug, expected in (("jiayi01", "周报"), ("yier01", "预算表")):
            status, body = self.call(
                "GET", f"/{slug}/api/state?surface=manage", token=self.token_for(slug)
            )

            self.assertEqual(status, 200, body)
            titles = " ".join(task["title"] for task in body["tasks"])
            self.assertIn(expected, titles)

    def test_a_token_from_one_meeting_is_refused_by_the_other(self) -> None:
        """The whole reason the guards travel with the service.

        A token names an actor inside one episode. If the other meeting
        accepted it, every roster check downstream would be asking the wrong
        room's door.
        """

        borrowed = self.token_for("jiayi01")
        status, _ = self.call(
            "GET", "/yier01/api/state?surface=manage", token=borrowed
        )

        self.assertIn(status, (401, 403))

    def test_an_unknown_meeting_is_a_404_not_somebody_elses_meeting(self) -> None:
        status, body = self.call("GET", "/nosuchmeeting01/api/state?surface=manage")

        self.assertEqual(status, 404)
        self.assertEqual(body.get("error"), "unknown_meeting")

    def test_the_root_lists_the_doors_without_opening_any(self) -> None:
        """Unauthenticated on purpose: a token is minted per meeting, so this
        is what somebody needs before they have one. It must therefore carry
        nothing from inside a meeting."""

        status, body = self.call("GET", "/api/meetings")

        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(entry["slug"] for entry in body["meetings"]),
            ["jiayi01", "yier01"],
        )
        for entry in body["meetings"]:
            self.assertEqual(set(entry) & {"tasks", "actors", "transcript"}, set())

    def test_the_bare_root_serves_the_index_page(self) -> None:
        """Not a redirect into some arbitrary meeting: with several of them
        there is no defensible "the" meeting to pick."""

        status, body = self.call("GET", "/")

        self.assertIn(status, (200, 503))
        if status == 200:
            self.assertIn("<!doctype html>", body.get("raw", "").lower())

    def test_the_bundle_is_served_once_for_every_meeting(self) -> None:
        """Not per meeting: the bytes are identical, and a per-meeting URL
        would only cost a cache entry each."""

        status, _ = self.call("GET", "/console/app.js")

        self.assertIn(status, (200, 503))

    def test_a_page_route_under_a_slug_serves_the_app(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/jiayi01/manage"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))

    def test_a_slug_root_sends_you_into_that_meeting(self) -> None:
        """Not to another meeting's task list, which is what a hard-coded
        /tasks would have done."""

        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/jiayi01/")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=10) as response:
                status, location = response.status, response.headers.get("Location")
        except urllib.error.HTTPError as error:
            status, location = error.code, error.headers.get("Location")

        self.assertEqual(status, 302)
        self.assertEqual(location, "/jiayi01/tasks")

    def test_a_write_lands_in_the_meeting_it_was_addressed_to(self) -> None:
        """The failure this guards against is silent: a write accepted by the
        wrong episode looks like success and shows up in another meeting."""

        token = self.token_for("jiayi01")
        status, body = self.call(
            "POST",
            "/jiayi01/api/action-items",
            {
                "title": "只属于甲一的会",
                "deliverable": "一页说明",
                "source_note": "会上口头提的",
                "message_id": "multi-add-1",
            },
            token,
        )
        self.assertEqual(status, 200, body)

        _, other = self.call(
            "GET", "/yier01/api/state?surface=manage", token=self.token_for("yier01")
        )
        self.assertNotIn(
            "只属于甲一的会", " ".join(task["title"] for task in other["tasks"])
        )
        _, mine = self.call(
            "GET", "/jiayi01/api/state?surface=manage", token=token
        )
        self.assertIn(
            "只属于甲一的会", " ".join(task["title"] for task in mine["tasks"])
        )


if __name__ == "__main__":
    unittest.main()
