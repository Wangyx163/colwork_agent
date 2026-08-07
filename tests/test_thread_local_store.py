from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from collab_agent.feishu_im import FeishuIM, RecordingTransport
from collab_agent.store import Database
from collab_agent.thread_local_store import ThreadLocalDatabase


SIM_TIME = "2026-03-02T09:00:00+08:00"


class SharedConnectionTests(unittest.TestCase):
    """Pins the failure this wrapper exists to prevent."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "threaded.sqlite3"
        bootstrap = Database(self.path)
        bootstrap.initialize()
        bootstrap.close()

    def test_a_shared_sqlite_connection_still_fails_across_threads(self) -> None:
        """The original bug: one connection reused from a second thread."""

        database = Database(self.path)
        self.addCleanup(database.close)
        im = FeishuIM(database, RecordingTransport())
        failures: list[BaseException] = []

        def bind_from_another_thread() -> None:
            try:
                im.bind_actor("参会者甲", "ou_aaa", sim_time=SIM_TIME)
            except BaseException as error:  # noqa: BLE001 - the point is to catch it
                failures.append(error)

        thread = threading.Thread(target=bind_from_another_thread)
        thread.start()
        thread.join()

        self.assertEqual(
            len(failures), 1, "sqlite3 must still refuse the cross-thread connection"
        )
        self.assertIn("same thread", str(failures[0]))

    def test_thread_local_database_lets_each_thread_write(self) -> None:
        database = ThreadLocalDatabase(
            lambda: Database(self.path, allow_cross_thread=True)
        )
        self.addCleanup(database.close)
        im = FeishuIM(database, RecordingTransport())
        failures: list[BaseException] = []
        barrier = threading.Barrier(4)

        def bind(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                im.bind_actor(
                    f"参会者{index}", f"ou_{index}", sim_time=SIM_TIME
                )
            except BaseException as error:  # noqa: BLE001
                failures.append(error)

        threads = [threading.Thread(target=bind, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(failures, [], "every thread must get its own connection")
        self.assertEqual(len(im.bindings()), 4)

    def test_each_thread_opens_exactly_one_connection(self) -> None:
        database = ThreadLocalDatabase(
            lambda: Database(self.path, allow_cross_thread=True)
        )
        self.addCleanup(database.close)
        im = FeishuIM(database, RecordingTransport())

        def bind_twice(index: int) -> None:
            im.bind_actor(f"a{index}", f"ou_a{index}", sim_time=SIM_TIME)
            im.bind_actor(f"b{index}", f"ou_b{index}", sim_time=SIM_TIME)

        threads = [threading.Thread(target=bind_twice, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(
            database.connection_count,
            4,
            "three worker threads plus the one that built the schema",
        )

    def test_inbound_click_recorded_on_one_thread_is_visible_on_another(self) -> None:
        """The callback thread writes; the worker thread must see it."""

        database = ThreadLocalDatabase(
            lambda: Database(self.path, allow_cross_thread=True)
        )
        self.addCleanup(database.close)
        im = FeishuIM(database, RecordingTransport())
        seen: list[int] = []

        def record() -> None:
            im.record_inbound_action(
                event_key="evt_cross",
                operator_open_id="ou_aaa",
                action_name="accept",
                effect_id="eff_1",
                raw_value={},
                sim_time=SIM_TIME,
            )

        def read() -> None:
            seen.append(len(im.pending_inbound_actions()))

        writer = threading.Thread(target=record)
        writer.start()
        writer.join(timeout=10)
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(timeout=10)

        self.assertEqual(seen, [1])

    def test_close_releases_every_connection_from_any_thread(self) -> None:
        database = ThreadLocalDatabase(
            lambda: Database(self.path, allow_cross_thread=True)
        )
        im = FeishuIM(database, RecordingTransport())

        thread = threading.Thread(
            target=lambda: im.bind_actor("甲", "ou_a", sim_time=SIM_TIME)
        )
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(database.connection_count, 2)

        database.close()

        self.assertEqual(database.connection_count, 0)


if __name__ == "__main__":
    unittest.main()
