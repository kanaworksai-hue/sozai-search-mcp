"""Thumbnails as MCP image content, so an agent can look at candidates before choosing."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from sozai import paths
from sozai.protocol import ToolContent, ToolError

MAX_BYTES = 400_000
FAB_LISTING = "https://www.fab.com/listings/"
FAB_WIDTH = 640  # Fab resizes each listing image to 320, 640, 960... px wide; 640 is about 35 KB


def _small_url(url: str) -> str:
    # ambientCG publishes WebP thumbnails next to the PNG ones; they are a fraction of the size
    if "struffelproductions.com" in url and "/256-PNG/" in url:
        return url.replace("/256-PNG/", "/256-WEBP/").rsplit(".", 1)[0] + ".webp"
    return url


def _mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def thumbnail_bytes(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme == "file":  # kit previews that `sozai ingest` wrote; nothing else on disk
        path = Path(url2pathname(parsed.path)).resolve()
        if not path.is_relative_to((paths.data_dir() / "catalog").resolve()):
            raise ToolError("local thumbnails are only read from the sozai catalog folder")
        if path.stat().st_size > MAX_BYTES:
            raise ToolError("thumbnail too large")
        return path.read_bytes()
    from sozai.fetch import download

    return download(_small_url(url), max_bytes=MAX_BYTES * 5).read_bytes()


def _fab_cache() -> Path:
    return paths.data_dir() / "cache" / "fab_previews.json"


def fab_preview_url(listing_url: str, page: str | None = None) -> str | None:
    """The image a Fab listing shows in link previews, at a size fit for an agent to look at.

    Read like a link preview: one listing page, only when an agent asks to see that asset, and the
    answer is remembered so the page is never read twice. Never used to collect previews in bulk.
    """
    if not listing_url.startswith(FAB_LISTING):
        return None
    cache = _fab_cache()
    try:
        known = json.loads(cache.read_text())
        known = known if isinstance(known, dict) else {}
    except (OSError, ValueError):  # missing, or damaged by an interrupted write: start over
        known = {}
    if listing_url in known:
        return known[listing_url]
    if page is None:
        from sozai.http import open_url

        with open_url(listing_url, timeout=20) as response:
            page = response.read(4_000_000).decode("utf-8", "replace")
    found = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if not found:
        return None
    og_image = found.group(1)
    folder = og_image.rsplit("/", 1)[0]  # .../gallery_images/<preview id>
    sizes = [(int(width), url) for width, url in re.findall(
        r'"width":\s*(\d+),\s*"height":\s*\d+,\s*"size":\s*\d+,\s*"url":\s*"(' + re.escape(folder) + r'/[^"]+)"', page)]
    fitting = [s for s in sizes if s[0] <= FAB_WIDTH]
    url = max(fitting)[1] if fitting else (min(sizes)[1] if sizes else og_image)
    known[listing_url] = url
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(known))
    tmp.replace(cache)
    return url


def view(assets: list[dict]) -> ToolContent:
    blocks: list[dict] = []
    shown = []
    for number, asset in enumerate(assets, 1):
        if asset.get("error"):
            blocks.append({"type": "text", "text": f"{number}. {asset['id']}: {asset['error']}"})
            continue
        size = asset.get("size_m")
        dims = asset.get("dims_m")
        label = f"{number}. {asset['id']} — {asset['title']}" + (
            f" — {size} m" if size else f" — {dims} m (axes unknown)" if dims else "")
        url = asset.get("thumbnail_url")
        if not url and asset.get("source") == "megascans" and os.environ.get("SOZAI_FAB_PREVIEWS") != "0":
            try:
                url = fab_preview_url(asset.get("url") or "")
            except Exception as error:  # Fab unreachable or blocking: say so, show the others
                blocks.append({"type": "text", "text": f"{label} (Fab preview unavailable: {error}; see {asset.get('url')})"})
                continue
        if not url:
            blocks.append({"type": "text", "text": label + " (no thumbnail)"})
            continue
        try:
            data = thumbnail_bytes(url)
        except Exception as error:  # a missing preview should not hide the others
            blocks.append({"type": "text", "text": f"{label} (thumbnail unavailable: {error})"})
            continue
        mime = _mime(data)
        if mime is None or len(data) > MAX_BYTES:
            blocks.append({"type": "text", "text": label + " (thumbnail not a small image)"})
            continue
        blocks.append({"type": "text", "text": label})
        blocks.append({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime})
        shown.append(asset["id"])
    if not blocks:
        raise ToolError("nothing to show")
    return ToolContent(blocks, {"shown": shown})
