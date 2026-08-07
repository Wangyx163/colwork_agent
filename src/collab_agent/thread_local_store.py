from __future__ import annotations

import threading
from typing import Any, Callable, Iterator


class ThreadLocalDatabase:
    """Hands each thread its own database connection.

    SQLite refuses to let one connection cross threads, and a psycopg
    connection is not safe for concurrent use either. The Feishu runtime is the
    first place in this project where several threads touch the database at
    once: lark delivers callbacks on its own threads while the worker thread
    drains the queue.

    Rather than serialise everything behind a lock, each thread gets its own
    connection -- the same shape the Web process and the Agent Worker process
    already have, just inside one process. Writers still contend on SQLite's
    file lock, which is the intended behaviour: `BEGIN IMMEDIATE` plus the
    driver's busy timeout is what keeps two writers from interleaving.

    The wrapped factory must return an already-initialised database; schema
    creation stays a one-time step on the main thread. For SQLite the factory
    must also pass `allow_cross_thread=True`, because `close` runs on whichever
    thread shuts the process down, not on the threads that opened connections.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._local = threading.local()
        self._opened: list[Any] = []
        self._lock = threading.Lock()

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len(self._opened)

    def _database(self) -> Any:
        database = getattr(self._local, "database", None)
        if database is None:
            database = self._factory()
            self._local.database = database
            with self._lock:
                self._opened.append(database)
        return database

    def transaction(self) -> Iterator[Any]:
        return self._database().transaction()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self._database().one(sql, params)

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        return self._database().all(sql, params)

    def append_audit(self, cursor: Any, **kwargs: Any) -> Any:
        return self._database().append_audit(cursor, **kwargs)

    def close(self) -> None:
        """Close every connection this wrapper handed out.

        Called from the shutdown path, which may not be any of the threads that
        opened a connection, so it must not go through the thread-local slot.

        A failure here is reported rather than swallowed: a connection that
        silently refused to close still holds the database file, and on Windows
        that surfaces much later as an unrelated "file in use" error.
        """

        with self._lock:
            opened, self._opened = self._opened, []
        errors: list[BaseException] = []
        for database in opened:
            try:
                database.close()
            except BaseException as error:  # noqa: BLE001 - close the rest first
                errors.append(error)
        self._local = threading.local()
        if errors:
            raise errors[0]
