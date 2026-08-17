from __future__ import annotations

import os
import unittest
from pathlib import Path
from uuid import uuid4

from collab_agent.evaluation import run_p0_evaluation
from collab_agent.web import workbench_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "p0_weekly.json"
DATABASE_URL = os.environ.get("COLWORK_TEST_POSTGRES_URL")


@unittest.skipUnless(
    DATABASE_URL,
    "set COLWORK_TEST_POSTGRES_URL to run the PostgreSQL integration test",
)
class PostgresAdapterTests(unittest.TestCase):
    def test_full_p0_on_postgres(self) -> None:
        import psycopg
        from psycopg import sql
        from collab_agent.postgres_store import database_url_for_schema

        schema_name = f"test_colwork_{uuid4().hex}"
        isolated_url = database_url_for_schema(
            DATABASE_URL, schema_name, create=True
        )
        service = None
        try:
            report, service = run_p0_evaluation(
                ROOT / "var" / "unused.sqlite3",
                FIXTURE,
                database_url=isolated_url,
                fresh=True,
            )
            self.assertTrue(report["passed"], report)
            self.assertTrue(
                all(gate["passed"] for gate in report["gates"].values()), report
            )
            database = service.db.one(
                "SELECT current_database() AS name, current_schema() AS schema_name"
            )
            self.assertEqual(database["name"], "colwork_agent")
            self.assertEqual(database["schema_name"], schema_name)
            state = workbench_state(service)
            self.assertTrue(state["tasks"])
            self.assertTrue(
                all(task["activity"] for task in state["tasks"] if task["required"])
            )
        finally:
            if service is not None:
                service.db.close()
            if not schema_name.startswith("test_colwork_"):
                raise AssertionError("refusing to drop a non-test schema")
            with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )

    def test_initialize_adds_relations_missing_from_an_existing_database(
        self,
    ) -> None:
        """A relation added to the schema must reach a database already built.

        The regression this covers only appears on a long-lived database: CI
        creates a fresh one every run and SQLite re-runs CREATE TABLE IF NOT
        EXISTS, so both hid the fact that `initialize` used to return the
        moment it saw `organizations`. Dropping a table from an initialized
        schema reproduces exactly what a later commit adding one looks like.
        """

        import psycopg
        from psycopg import sql
        from collab_agent.postgres_store import (
            PostgresDatabase,
            database_url_for_schema,
        )

        schema_name = f"test_colwork_{uuid4().hex}"
        isolated_url = database_url_for_schema(
            DATABASE_URL, schema_name, create=True
        )
        schema_path = ROOT / "db" / "postgres_schema.sql"
        dropped = ("assignment_handoffs", "action_item_link_lookup")
        try:
            database = PostgresDatabase(isolated_url, schema_path=schema_path)
            database.initialize()
            database.close()

            with psycopg.connect(isolated_url, autocommit=True) as connection:
                connection.execute("DROP INDEX action_item_link_lookup")
                connection.execute("DROP TABLE assignment_handoffs")
                for relation in dropped:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT to_regclass(%s)", (relation,)
                        ).fetchone()[0]
                    )

            database = PostgresDatabase(isolated_url, schema_path=schema_path)
            database.initialize()
            database.close()

            with psycopg.connect(isolated_url, autocommit=True) as connection:
                for relation in dropped:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT to_regclass(%s)", (relation,)
                        ).fetchone()[0],
                        f"{relation} was not restored by initialize()",
                    )
        finally:
            if not schema_name.startswith("test_colwork_"):
                raise AssertionError("refusing to drop a non-test schema")
            with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
