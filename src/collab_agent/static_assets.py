"""Serve the built single-page bundle from the same process as the workbench.

The bundle is committed rather than built on demand, so cloning the repository
and running Python is enough to see the pages -- no Node, and CI stays a pure
Python job. `web/` holds the source; `npm run build` writes here.

One bundle backs every React page. Two bundles would put React in the
repository twice and double what each rebuild adds to git history, so the
pages share `/console/` for their assets and differ only in the route the
client reads off `location.pathname`.
"""

from __future__ import annotations

from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parent / "static" / "console"

#: Where the bundle's own assets live. Vite is told this same string as `base`,
#: so the emitted `<script src>` is absolute and works from any page route.
ASSET_PREFIX = "/console"

#: Page routes the bundle answers. Each serves index.html; the client decides
#: what to render. Registered here so the server and the tests agree on one list.
PAGE_ROUTES = ("/observatory", "/manage")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class AssetMissing(FileNotFoundError):
    """The bundle is absent, which means it was never built or not committed."""


def bundle_exists() -> bool:
    return (BUNDLE_ROOT / "index.html").is_file()


def serves(request_path: str) -> bool:
    """Whether the bundle owns this path, page route or asset alike."""

    if request_path == ASSET_PREFIX or request_path.startswith(
        f"{ASSET_PREFIX}/"
    ):
        return True
    return any(
        request_path == route or request_path.startswith(f"{route}/")
        for route in PAGE_ROUTES
    )


def read_asset(request_path: str) -> tuple[bytes, str]:
    """Resolve a served path to a file inside the bundle.

    Path traversal is refused by resolving the candidate and requiring it to
    stay under the bundle root -- a served directory reachable with `..` would
    hand out the rest of the source tree.
    """

    relative = request_path
    for prefix in (ASSET_PREFIX, *PAGE_ROUTES):
        if relative == prefix or relative.startswith(f"{prefix}/"):
            relative = relative.removeprefix(prefix)
            break
    relative = relative.lstrip("/")
    if not relative or relative.endswith("/"):
        relative = "index.html"

    candidate = (BUNDLE_ROOT / relative).resolve()
    root = BUNDLE_ROOT.resolve()
    if root not in candidate.parents and candidate != root:
        raise AssetMissing(f"{request_path} escapes the bundle")
    if not candidate.is_file():
        # A single-page app owns its own routing, so an unknown path that is
        # not an asset is a client route rather than a miss.
        if candidate.suffix:
            raise AssetMissing(f"{request_path} is not in the bundle")
        candidate = root / "index.html"
        if not candidate.is_file():
            raise AssetMissing("the bundle has not been built")

    content_type = CONTENT_TYPES.get(
        candidate.suffix, "application/octet-stream"
    )
    return candidate.read_bytes(), content_type


MISSING_BUNDLE_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>页面未构建</title>
<body style="font:15px system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>页面还没构建</h1>
<p>页面代码在 <code>web/</code>，构建产物应该提交在
<code>src/collab_agent/static/console/</code>。</p>
<pre style="background:#f2f4f6;padding:1rem;border-radius:6px">cd web
npm install
npm run build</pre>
</body>
""".encode("utf-8")
