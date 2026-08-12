from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from collab_agent.auth import (
    OPERATOR_TOKEN_ENV,
    operator_token_matches,
    resolve_operator_token,
)
from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database
from collab_agent.thread_local_store import ThreadLocalDatabase
from collab_agent.web import build_console_server, console_for


def extraction_for(quote: str) -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
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


class OperatorTokenTests(unittest.TestCase):
    """The credential itself. A secret compared with == leaks its length and
    prefix through timing, which is cheap to avoid and awkward to retrofit."""

    def test_a_configured_token_is_used_as_given(self) -> None:
        os.environ[OPERATOR_TOKEN_ENV] = "  my-own-secret  "
        self.addCleanup(os.environ.pop, OPERATOR_TOKEN_ENV, None)

        self.assertEqual(resolve_operator_token(), ("my-own-secret", "env"))

    def test_without_one_a_token_is_generated_rather_than_left_open(self) -> None:
        """A default of "open" would mean every deployment that never read the
        docs serves every meeting's audit trail to whoever finds the URL."""

        os.environ.pop(OPERATOR_TOKEN_ENV, None)
        token, source = resolve_operator_token()

        self.assertEqual(source, "generated")
        self.assertGreaterEqual(len(token), 24)
        self.assertNotEqual(token, resolve_operator_token()[0])

    def test_the_header_may_carry_it_with_or_without_bearer(self) -> None:
        self.assertTrue(operator_token_matches("abc", "Bearer abc"))
        self.assertTrue(operator_token_matches("abc", "abc"))

    def test_nothing_matches_when_no_token_is_expected(self) -> None:
        """An empty expected token must never mean "allow anything"."""

        self.assertFalse(operator_token_matches("", "Bearer anything"))
        self.assertFalse(operator_token_matches("", ""))

    def test_a_wrong_or_missing_token_does_not_match(self) -> None:
        self.assertFalse(operator_token_matches("abc", "Bearer abd"))
        self.assertFalse(operator_token_matches("abc", None))
        self.assertFalse(operator_token_matches("abc", "Bearer "))


class ObservatoryAccessTests(unittest.TestCase):
    """The Observatory reads across every meeting, so no meeting's role can
    authorize it.

    While it was gated on "are you this meeting's coordinator" it also took the
    episode to show as a query parameter. That was harmless when one process
    served one meeting -- the only episode you could name was your own -- and
    became a cross-meeting read the moment one process served several. The
    regression test for that is here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        path = root / "console.sqlite3"
        bootstrap = Database(path, allow_cross_thread=True)
        bootstrap.initialize()
        bootstrap.close()
        cls.db = ThreadLocalDatabase(lambda: Database(path, allow_cross_thread=True))

        consoles = []
        cls.actors: dict[str, str] = {}
        cls.episodes: dict[str, dict[str, str]] = {}
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
                organization_name="观测台测试团队",
                coordinator_name=coordinator,
                participant_names=[coordinator, f"{coordinator}的同事"],
            )
            consoles.append(console_for(service, slug=slug, title=slug))
            cls.actors[slug] = service.aggregator_actor_id
            cls.episodes[slug] = {
                "episode_id": service.episode_id,
                "run_id": service.run_id,
            }

        cls.token = "test-operator-token"
        cls.server = build_console_server(
            consoles, host="127.0.0.1", port=0, operator_token=cls.token
        )
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

    def call(self, path: str, token: str = "", *, follow: bool = True):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("authorization", f"Bearer {token}")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return None

        opener = (
            urllib.request.build_opener()
            if follow
            else urllib.request.build_opener(NoRedirect)
        )
        try:
            with opener.open(request, timeout=10) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    return response.status, json.loads(raw or "{}"), response.headers
                except json.JSONDecodeError:
                    return response.status, {"raw": raw[:200]}, response.headers
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(raw or "{}"), error.headers
            except json.JSONDecodeError:
                return error.code, {"raw": raw[:200]}, error.headers

    def meeting_token(self, slug: str) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/{slug}/api/session",
            data=json.dumps({"actor_id": self.actors[slug]}).encode(),
            method="POST",
        )
        request.add_header("content-type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["token"]

    # ---- the gate ------------------------------------------------------

    def test_without_a_token_the_data_is_refused(self) -> None:
        status, body, _ = self.call("/api/observatory")

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "OPERATOR_REQUIRED")

    def test_a_wrong_token_is_refused(self) -> None:
        status, _, _ = self.call("/api/observatory", "not-the-token")

        self.assertEqual(status, 403)

    def test_the_operator_token_opens_it(self) -> None:
        status, body, _ = self.call("/api/observatory", self.token)

        self.assertEqual(status, 200)
        self.assertEqual(body["schema_version"], "observatory.v1")

    def test_a_meeting_token_is_not_an_operator_token(self) -> None:
        """The whole point of the move: being a coordinator of one meeting is
        not authority over a reading of all of them."""

        status, _, _ = self.call("/api/observatory", self.meeting_token("jiayi01"))

        self.assertEqual(status, 403)

    def test_a_coordinator_cannot_read_another_meetings_run(self) -> None:
        """The regression test for the hole. This exact request -- a valid
        coordinator token plus the other meeting's ids in the query string --
        returned that meeting's complete audit trail."""

        other = self.episodes["yier01"]
        status, _, _ = self.call(
            f"/api/observatory?episode_id={other['episode_id']}"
            f"&run_id={other['run_id']}",
            self.meeting_token("jiayi01"),
        )

        self.assertEqual(status, 403)

    # ---- one door ------------------------------------------------------

    def test_there_is_no_per_meeting_observatory_api(self) -> None:
        status, _, _ = self.call("/jiayi01/api/observatory", self.token)

        self.assertEqual(status, 404)

    def test_a_meeting_prefixed_page_redirects_to_the_one_at_the_root(self) -> None:
        """One URL rather than one per meeting, each looking like it showed
        that meeting."""

        status, _, headers = self.call("/jiayi01/observatory", follow=False)

        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/observatory")

    def test_the_page_shell_is_open_so_the_token_can_be_typed_in(self) -> None:
        """Gating the shell too would be a login screen you cannot reach."""

        status, _, _ = self.call("/observatory")

        self.assertIn(status, (200, 503))

    def test_diagnostics_is_the_same_door(self) -> None:
        status, _, _ = self.call("/diagnostics")

        self.assertIn(status, (200, 503))

    # ---- what an operator is for ----------------------------------------

    def test_an_operator_may_read_any_meeting(self) -> None:
        """Not a leftover of the old hole: reading across meetings is what the
        surface is for, which is why it needed a credential that spans them."""

        for slug in ("jiayi01", "yier01"):
            episode = self.episodes[slug]
            status, body, _ = self.call(
                f"/api/observatory?episode_id={episode['episode_id']}"
                f"&run_id={episode['run_id']}",
                self.token,
            )

            self.assertEqual(status, 200)
            self.assertEqual(body["run"]["run_id"], episode["run_id"])

    def test_it_defaults_to_the_newest_run_rather_than_a_served_one(self) -> None:
        """A meeting imported from chat exists in the database before any
        process serves it, and that is exactly when somebody wants to look."""

        status, body, _ = self.call("/api/observatory", self.token)

        self.assertEqual(status, 200)
        self.assertTrue(body["run"]["run_id"])
        self.assertGreaterEqual(len(body["runs"]), 2)


if __name__ == "__main__":
    unittest.main()
