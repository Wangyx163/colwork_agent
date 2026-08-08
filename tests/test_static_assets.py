from __future__ import annotations

import unittest
from pathlib import Path

from collab_agent.static_assets import (
    OBSERVATORY_ROOT,
    AssetMissing,
    bundle_exists,
    read_asset,
)


ROOT = Path(__file__).resolve().parents[1]


class BundleShippedTests(unittest.TestCase):
    """The build output is committed on purpose.

    A reviewer should be able to clone and run the workbench with Python
    alone; if the bundle went missing the page would 503 with no sign that
    anything was ever meant to be there.
    """

    def test_the_bundle_is_present(self) -> None:
        self.assertTrue(
            bundle_exists(),
            "run `npm run build` in web/ and commit "
            "src/collab_agent/static/observatory/",
        )

    def test_the_entry_files_are_the_stable_names_vite_was_told_to_emit(self) -> None:
        """Hashed filenames would leave every previous build behind in git."""

        for name in ("index.html", "app.js", "app.css"):
            self.assertTrue(
                (OBSERVATORY_ROOT / name).is_file(), f"{name} missing from bundle"
            )

    def test_the_bundle_is_not_gitignored(self) -> None:
        # Comments are stripped first: .gitignore explains *why* the bundle is
        # committed by naming its path, and matching that prose would make
        # this test fail on the very sentence that documents the intent.
        rules = [
            line.strip()
            for line in (ROOT / ".gitignore")
            .read_text(encoding="utf-8-sig")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertIn("web/dist/", rules, "the source build dir stays ignored")
        self.assertFalse(
            [rule for rule in rules if "collab_agent/static" in rule],
            "the shipped bundle must stay committed",
        )


class AssetResolutionTests(unittest.TestCase):
    def test_the_bare_route_serves_the_page(self) -> None:
        body, content_type = read_asset("/observatory")

        self.assertIn(b"<!doctype html>", body.lower())
        self.assertEqual(content_type, "text/html; charset=utf-8")

    def test_assets_get_their_own_content_type(self) -> None:
        _, js = read_asset("/observatory/app.js")
        _, css = read_asset("/observatory/app.css")

        self.assertEqual(js, "text/javascript; charset=utf-8")
        self.assertEqual(css, "text/css; charset=utf-8")

    def test_a_client_route_falls_back_to_the_page(self) -> None:
        """The bundle owns its routing; an extensionless miss is not a 404."""

        body, content_type = read_asset("/observatory/run/run_p0")

        self.assertIn(b"<!doctype html>", body.lower())
        self.assertEqual(content_type, "text/html; charset=utf-8")

    def test_a_missing_asset_is_refused_rather_than_answered_with_html(self) -> None:
        with self.assertRaises(AssetMissing):
            read_asset("/observatory/does-not-exist.js")

    def test_path_traversal_cannot_reach_the_source_tree(self) -> None:
        for attempt in (
            "/observatory/../../service.py",
            "/observatory/../../../.env.local",
            "/observatory/..%2f..%2fservice.py",
        ):
            with self.assertRaises(AssetMissing, msg=attempt):
                read_asset(attempt)


if __name__ == "__main__":
    unittest.main()
