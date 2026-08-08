"""Serve the built Observatory bundle from the same process as the workbench.

The bundle is committed rather than built on demand, so cloning the repository
and running Python is enough to see the page -- no Node, and CI stays a pure
Python job. `web/` holds the source; `npm run build` writes here.
"""

from __future__ import annotations

from pathlib import Path


OBSERVATORY_ROOT = Path(__file__).resolve().parent / "static" / "observatory"

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
    return (OBSERVATORY_ROOT / "index.html").is_file()


def read_asset(request_path: str) -> tuple[bytes, str]:
    """Resolve a `/observatory/...` path to a file inside the bundle.

    Path traversal is refused by resolving the candidate and requiring it to
    stay under the bundle root -- a served directory reachable with `..` would
    hand out the rest of the source tree.
    """

    relative = request_path.removeprefix("/observatory").lstrip("/")
    if not relative or relative.endswith("/"):
        relative = "index.html"

    candidate = (OBSERVATORY_ROOT / relative).resolve()
    root = OBSERVATORY_ROOT.resolve()
    if root not in candidate.parents and candidate != root:
        raise AssetMissing(f"{request_path} escapes the bundle")
    if not candidate.is_file():
        # A single-page app owns its own routing, so an unknown path that is
        # not an asset is a client route rather than a miss.
        if candidate.suffix:
            raise AssetMissing(f"{request_path} is not in the bundle")
        candidate = root / "index.html"
        if not candidate.is_file():
            raise AssetMissing("the Observatory bundle has not been built")

    content_type = CONTENT_TYPES.get(
        candidate.suffix, "application/octet-stream"
    )
    return candidate.read_bytes(), content_type


MISSING_BUNDLE_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>Observatory \xe6\x9c\xaa\xe6\x9e\x84\xe5\xbb\xba</title>
<body style="font:15px system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>Observatory \xe8\xbf\x98\xe6\xb2\xa1\xe6\x9e\x84\xe5\xbb\xba</h1>
<p>\xe9\xa1\xb5\xe9\x9d\xa2\xe4\xbb\xa3\xe7\xa0\x81\xe5\x9c\xa8 <code>web/</code>\xef\xbc\x8c
\xe6\x9e\x84\xe5\xbb\xba\xe4\xba\xa7\xe7\x89\xa9\xe5\xba\x94\xe8\xaf\xa5\xe6\x8f\x90\xe4\xba\xa4\xe5\x9c\xa8
<code>src/collab_agent/static/observatory/</code>\xe3\x80\x82</p>
<pre style="background:#f2f4f6;padding:1rem;border-radius:6px">cd web
npm install
npm run build</pre>
</body>
"""
