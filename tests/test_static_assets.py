from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from collab_agent.static_assets import (
    ASSET_PREFIX,
    BUNDLE_ROOT,
    PAGE_ROUTES,
    AssetMissing,
    bundle_exists,
    read_asset,
    serves,
)


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


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
            "src/collab_agent/static/console/",
        )

    def test_the_entry_files_are_the_stable_names_vite_was_told_to_emit(self) -> None:
        """Hashed filenames would leave every previous build behind in git."""

        for name in ("index.html", "app.js", "app.css"):
            self.assertTrue(
                (BUNDLE_ROOT / name).is_file(), f"{name} missing from bundle"
            )

    def test_one_bundle_backs_every_page(self) -> None:
        """A second bundle would put React in the repository twice."""

        self.assertEqual(
            sorted(path.name for path in BUNDLE_ROOT.parent.iterdir()),
            ["console"],
        )

    def test_the_markup_points_at_the_shared_asset_prefix(self) -> None:
        """Vite's `base` and ASSET_PREFIX have to agree or nothing loads."""

        markup = (BUNDLE_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn(f'src="{ASSET_PREFIX}/app.js"', markup)
        self.assertIn(f'href="{ASSET_PREFIX}/app.css"', markup)

    def test_the_bundle_was_built_from_the_committed_sources(self) -> None:
        """Committed build output drifts, and the drift is invisible.

        Edit a source, forget to rebuild, commit: the diff shows the change and
        the page keeps serving the previous UI. The cost lands on whoever opens
        the page next, who sees the old screen and concludes the work was never
        done -- which is exactly what happened.

        `npm run build` stamps the bundle with a hash of what it was built
        from; this recomputes it. No Node required, so CI keeps its pure-Python
        job.
        """

        manifest_path = BUNDLE_ROOT / "build-manifest.json"
        self.assertTrue(
            manifest_path.is_file(),
            "no build stamp: rebuild with `npm run build` in web/",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        digest = hashlib.sha256()
        # Sorted on the relative posix path as plain strings. Sorting Path
        # objects is case-insensitive on Windows and case-sensitive elsewhere,
        # which would hash the same files in a different order than the build.
        files = sorted(
            path.relative_to(WEB).as_posix()
            for root in ("src", "index.html", "vite.config.ts", "tsconfig.app.json")
            for path in (
                (WEB / root).rglob("*") if (WEB / root).is_dir() else [WEB / root]
            )
            if path.is_file() and not path.name.endswith(".test.ts")
        )
        for name in files:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update((WEB / name).read_bytes())
            digest.update(b"\0")

        self.assertEqual(len(files), manifest["file_count"])
        self.assertEqual(
            digest.hexdigest(),
            manifest["source_sha256"],
            "web/ has changed since the bundle was built -- run `npm run build`",
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


class RouteOwnershipTests(unittest.TestCase):
    def test_every_page_route_and_the_asset_prefix_are_claimed(self) -> None:
        for path in (ASSET_PREFIX, *PAGE_ROUTES):
            self.assertTrue(serves(path), path)
            self.assertTrue(serves(f"{path}/app.js"), path)

    def test_the_api_is_never_swallowed_by_the_page_routes(self) -> None:
        """An API path caught here would answer JSON callers with HTML."""

        for path in ("/", "/api/state", "/api/session", "/api/observatory"):
            self.assertFalse(serves(path), path)

    def test_a_lookalike_prefix_is_not_claimed(self) -> None:
        self.assertFalse(serves("/manageable"))
        self.assertFalse(serves("/consoles/app.js"))


class AssetResolutionTests(unittest.TestCase):
    def test_every_page_route_serves_the_page(self) -> None:
        for route in PAGE_ROUTES:
            body, content_type = read_asset(route)

            self.assertIn(b"<!doctype html>", body.lower(), route)
            self.assertEqual(content_type, "text/html; charset=utf-8", route)

    def test_assets_get_their_own_content_type(self) -> None:
        _, js = read_asset(f"{ASSET_PREFIX}/app.js")
        _, css = read_asset(f"{ASSET_PREFIX}/app.css")

        self.assertEqual(js, "text/javascript; charset=utf-8")
        self.assertEqual(css, "text/css; charset=utf-8")

    def test_a_client_route_falls_back_to_the_page(self) -> None:
        """The bundle owns its routing; an extensionless miss is not a 404."""

        body, content_type = read_asset("/observatory/run/run_p0")

        self.assertIn(b"<!doctype html>", body.lower())
        self.assertEqual(content_type, "text/html; charset=utf-8")

    def test_a_missing_asset_is_refused_rather_than_answered_with_html(self) -> None:
        with self.assertRaises(AssetMissing):
            read_asset(f"{ASSET_PREFIX}/does-not-exist.js")

    def test_path_traversal_cannot_reach_the_source_tree(self) -> None:
        for attempt in (
            f"{ASSET_PREFIX}/../../service.py",
            f"{ASSET_PREFIX}/../../../.env.local",
            "/manage/../../service.py",
            f"{ASSET_PREFIX}/..%2f..%2fservice.py",
        ):
            with self.assertRaises(AssetMissing, msg=attempt):
                read_asset(attempt)


if __name__ == "__main__":
    unittest.main()
