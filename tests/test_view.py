import base64
import json
import os
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

from sozai import view
from sozai.protocol import StdioServer
from test_fetch import kit_index, make_kit  # noqa: F401  (fixture)
from test_measure import png


def test_view_returns_numbered_images_and_skips_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    image = tmp_path / "home" / "catalog" / "previews" / "a.png"  # where `sozai ingest` puts kit previews
    image.parent.mkdir(parents=True)
    image.write_bytes(png(16, 16))
    elsewhere = tmp_path / "private.png"
    elsewhere.write_bytes(png(16, 16))
    assets = [
        {"id": "kenney:k/a", "title": "A", "size_m": [1, 2, 3], "thumbnail_url": image.as_uri()},
        {"id": "kenney:k/b", "title": "B", "thumbnail_url": None},
        {"id": "polyhaven:c", "title": "C", "thumbnail_url": "https://cdn.test/c.webp"},
        {"id": "kenney:k/d", "title": "D", "thumbnail_url": elsewhere.as_uri()},
        {"id": "nope:x", "error": "No asset 'nope:x'."},
    ]
    monkeypatch.setattr("sozai.fetch.download", lambda url, md5=None, **kw: (_ for _ in ()).throw(OSError("offline")))
    result = view.view(assets)
    kinds = [b["type"] for b in result.blocks]
    assert kinds == ["text", "image", "text", "text", "text", "text"]
    assert result.blocks[0]["text"] == "1. kenney:k/a — A — [1, 2, 3] m"
    assert base64.b64decode(result.blocks[1]["data"])[:4] == b"\x89PNG" and result.blocks[1]["mimeType"] == "image/png"
    assert "no thumbnail" in result.blocks[2]["text"] and "offline" in result.blocks[3]["text"]
    assert "only read from the sozai catalog" in result.blocks[4]["text"]
    assert result.blocks[5]["text"] == "5. nope:x: No asset 'nope:x'."
    assert result.structured == {"shown": ["kenney:k/a"]}


def test_large_previews_are_shrunk_or_linked(monkeypatch):
    big = png(16, 16) + bytes(view.MAX_BYTES)  # a PNG over the size an image block may have
    monkeypatch.setattr(view, "thumbnail_bytes", lambda url: big)
    assets = [{"id": "opensourceavatars:r1/a", "title": "A", "thumbnail_url": "https://arweave.net/x"}]
    monkeypatch.setattr(view, "_shrink", lambda data: None)  # no Pillow: the link instead
    assert view.view(assets).blocks[0]["text"].endswith("(preview too large to show here: https://arweave.net/x)")
    monkeypatch.setattr(view, "_shrink", lambda data: png(4, 4))
    blocks = view.view(assets).blocks
    assert [b["type"] for b in blocks] == ["text", "image"] and blocks[1]["mimeType"] == "image/png"


def test_ambientcg_thumbnails_use_webp():
    url = "https://acg-media.struffelproductions.com/file/ambientCG-Web/media/thumbnail/256-PNG/Bricks059.png"
    assert view._small_url(url).endswith("/256-WEBP/Bricks059.webp")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_view_assets_over_stdio(kit_index, tmp_path):  # noqa: F811
    params = StdioServerParameters(command=sys.executable, args=["-m", "sozai"],
                                   env={**os.environ, "SOZAI_HOME": str(tmp_path / "home")})
    with anyio.fail_after(30):
        async with Client(params, read_timeout_seconds=20) as client:
            result = await client.call_tool("view_assets", {"asset_ids": ["kenney:test-kit/crate-wide"]})
            assert not result.is_error
            assert [c.type for c in result.content] == ["text", "image"]
            assert result.content[1].mime_type == "image/png"


def test_previews_over_2_mb_are_downloaded_to_shrink_only_with_pillow(tmp_path, monkeypatch):
    import io

    from sozai import fetch

    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    out = io.BytesIO()
    Image.effect_noise((1100, 1100), 64).convert("RGB").save(out, "PNG")  # ~3 MB, like the big avatar previews
    big = out.getvalue()
    assert len(big) > 2_000_000

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(big))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    requests = []
    monkeypatch.setattr(fetch, "open_url", lambda url, timeout: requests.append(url) or Response(big))
    assets = [{"id": "opensourceavatars:r1/a", "title": "A", "thumbnail_url": "https://arweave.net/x"}]
    monkeypatch.setattr(view, "_can_shrink", lambda: False)  # no Pillow: not downloaded, just linked
    blocks = view.view(assets).blocks
    assert len(blocks) == 1 and blocks[0]["text"].endswith("(preview too large to show here: https://arweave.net/x)")
    assert not list((tmp_path / "home" / "cache").glob("**/*.*"))  # nothing cached
    monkeypatch.setattr(view, "_can_shrink", lambda: True)
    blocks = view.view(assets).blocks
    assert [b["type"] for b in blocks] == ["text", "image"] and blocks[1]["mimeType"] == "image/webp"
    assert len(base64.b64decode(blocks[1]["data"])) < view.MAX_BYTES
    view.view(assets)
    assert len(requests) == 2  # the shrunk-from file is cached: the second look downloads nothing
