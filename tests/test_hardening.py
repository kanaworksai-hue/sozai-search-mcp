"""Untrusted data must not reach outside its folders, and one bad input must not break a session."""
import io
import json
import sqlite3
import sys
import time
import urllib.request
import zipfile

import pytest

from sozai import bundle, fetch, http, index, semantic, server, view
from sozai.protocol import StdioServer, Tool
from sozai.search import size_m
from sozai.sources import megascans

from test_fetch import write_gltf


def polyhaven_model(files_json, monkeypatch, served):
    monkeypatch.setattr(fetch.Http, "get_json", lambda self, url: files_json)
    monkeypatch.setattr(fetch, "download", lambda url, md5=None: served[url.rsplit("/", 1)[1]])
    return {"id": "polyhaven:crate", "source": "polyhaven", "source_id": "crate", "title": "Crate", "kind": "model",
            "licence": "CC0-1.0", "credit": "Powered by Poly Haven", "url": "u", "size_m": [0.5, 0.5, 0.5],
            "downloads": {"files_api": "https://api.polyhaven.com/files/crate"}}


@pytest.fixture
def crate(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    src = tmp_path / "src"
    write_gltf(src, "crate_1k")
    served = {"crate_1k.gltf": src / "crate_1k.gltf", "crate_1k.bin": src / "crate_1k.bin",
              "crate_1k_diff.png": src / "textures" / "crate_1k_diff.png"}
    include = {"crate_1k.bin": {"url": "https://dl.test/crate_1k.bin"},
               "textures/crate_1k_diff.png": {"url": "https://dl.test/crate_1k_diff.png"}}
    files_json = {"gltf": {"1k": {"gltf": {"url": "https://dl.test/crate_1k.gltf", "include": include}}}}
    return polyhaven_model(files_json, monkeypatch, served), files_json


def test_include_paths_cannot_leave_dest(tmp_path, crate):
    asset, files_json = crate
    files_json["gltf"]["1k"]["gltf"]["include"]["../../outside.txt"] = {"url": "https://dl.test/crate_1k.bin"}
    with pytest.raises(fetch.ToolError, match="unsafe path"):
        fetch.fetch_asset(asset, str(tmp_path / "proj" / "assets"), resolution="1k", fmt="gltf")
    assert not (tmp_path / "proj" / "outside.txt").exists() and not (tmp_path / "outside.txt").exists()
    assert [p.name for p in (tmp_path / "proj" / "assets").iterdir()] == []  # the staging folder is gone too


def test_source_ids_cannot_leave_dest(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.tres", "x")
    monkeypatch.setattr(fetch, "download", lambda url, md5=None: archive)
    asset = {"id": "ambientcg:x", "source": "ambientcg", "source_id": "../../escape", "title": "X", "kind": "material",
             "licence": "CC0-1.0", "url": "u",
             "downloads": {"zips": [{"attributes": "1K-JPG", "url": "https://ambientcg.com/get?file=a.zip"}]}}
    out = fetch.fetch_asset(asset, str(tmp_path / "proj" / "assets"), resolution="1k")
    assert out["files"] == ["escape_1K-JPG/a.tres"] and not list(tmp_path.glob("**/escape_1K-JPG/../../*.tres"))


def test_fetch_never_deletes_existing_files(tmp_path, crate):
    asset, _ = crate
    dest = tmp_path / "out"
    fetch.fetch_asset(asset, str(dest), resolution="1k", fmt="gltf")
    (dest / "crate_1k" / "my_edits.blend").write_text("mine")
    fetch.fetch_asset(asset, str(dest), resolution="1k", fmt="glb")
    fetch.fetch_asset(asset, str(dest), resolution="1k", fmt="gltf")  # again over the same folder
    assert (dest / "crate_1k" / "my_edits.blend").read_text() == "mine"
    assert (dest / "crate_1k.glb").is_file() and not list(dest.glob(".sozai-*"))


def test_fetch_wants_an_absolute_dest(crate):
    asset, _ = crate
    with pytest.raises(fetch.ToolError, match="absolute"):
        fetch.fetch_asset(asset, "assets", resolution="1k")


def test_kit_records_cannot_copy_files_from_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    kit = tmp_path / "kit.zip"
    with zipfile.ZipFile(kit, "w") as zf:
        zf.writestr("a.glb", "x")
    asset = {"id": "kenney:k/a", "source": "kenney", "source_id": "k/a", "title": "A", "kind": "model",
             "licence": "CC0-1.0", "url": "u", "attributes": {},
             "downloads": {"kit_zip": str(kit), "member": "a.glb", "converted": str(secret)}}
    with pytest.raises(fetch.ToolError, match="outside the sozai data folder"):
        fetch.fetch_asset(asset, str(tmp_path / "out"))
    assert not (tmp_path / "out" / "k_a.glb").exists()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_download_names_stay_in_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fetch, "open_url", lambda url, timeout: FakeResponse(b"data"))
    path = fetch.download("https://ambientcg.com/get?file=../../../escaped.txt")
    assert path.is_relative_to(tmp_path / "home" / "cache") and path.name == "escaped.txt"
    assert not list(tmp_path.glob("**/escaped.txt"))[:-1]  # exactly one, the cached one
    assert not list((tmp_path / "home" / "cache").glob("**/*.part"))


def test_download_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fetch, "open_url", lambda url, timeout: FakeResponse(b"x" * 5000))
    with pytest.raises(fetch.ToolError, match="larger than"):
        fetch.download("https://dl.polyhaven.org/big.exr", max_bytes=1000)
    assert not list((tmp_path / "home" / "cache").glob("**/*"))[1:]  # only the empty digest folder


def test_urls_are_limited_to_known_https_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    http.check_url("https://dl.polyhaven.org/file/a.glb")
    http.check_url("https://acg-media.struffelproductions.com/x.png")
    for bad in ("http://dl.polyhaven.org/a", "file:///etc/passwd", "https://evil.example/a",
                "https://polyhaven.com.evil.example/a", "https://notpolyhaven.com/a"):
        with pytest.raises(http.UnsafeURL):
            http.check_url(bad)
    handler = http._CheckedRedirects()
    request = urllib.request.Request("https://api.polyhaven.com/files/a")
    with pytest.raises(http.UnsafeURL):
        handler.redirect_request(request, None, 302, "Found", {}, "http://api.polyhaven.com/files/a")
    with pytest.raises(fetch.ToolError, match="non-https"):
        fetch.download("http://dl.polyhaven.org/a.glb")
    assert not (tmp_path / "home").exists()  # refused before any folder was made


def test_semantic_worker_is_isolated_and_gives_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    assert semantic.Client().command[1:3] == ["-I", "-m"]  # the project folder is not on its import path
    monkeypatch.setattr(semantic, "START_TIMEOUT", 0.5)
    client = semantic.Client([sys.executable, "-c", "import time; time.sleep(30)"])  # never says ready
    with pytest.raises(RuntimeError, match="failed to start"):
        client.search(["crate"])
    assert client.process is None
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="recently"):
        client.search(["crate"])  # not another slow start
    assert time.monotonic() - started < 0.2


def test_semantic_worker_query_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(semantic, "QUERY_TIMEOUT", 0.5)
    hangs = "import json, time; print(json.dumps({'ready': True}), flush=True); time.sleep(30)"
    client = semantic.Client([sys.executable, "-c", hangs])
    with pytest.raises(TimeoutError):
        client.search(["crate"])
    assert client.process is None


def test_one_bad_message_does_not_end_the_session():
    server_ = StdioServer("t", "0", "", [Tool("echo", "", {"type": "object", "properties": {}}, lambda: {"ok": 1})])
    lines = [json.dumps({"jsonrpc": "2.0", "id": "\ud800", "method": "ping"}).encode(),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode()]
    out = io.BytesIO()
    server_.run(io.BytesIO(b"\n".join(lines) + b"\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == ["\ud800", 2]


def test_index_path_with_special_characters(tmp_path):
    for name in ("we#ird", "pct%41") if sys.platform == "win32" else ("we#ird", "q?mark", "pct%41"):
        path = tmp_path / name / "index.sqlite"
        index.build([], path)
        assert index.read_meta(index.connect(path))["count"] == 0
    assert not (tmp_path / "we").exists()


def test_bad_cached_data_zip_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    cached = tmp_path / "home" / "cache" / "x" / "data.zip"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"not a zip")
    monkeypatch.setattr(fetch, "download", lambda url, md5=None: cached)
    with pytest.raises(zipfile.BadZipFile):
        bundle.install("https://github.com/x/data.zip", log=lambda m: None)
    assert not cached.exists() and not list((tmp_path / "home").glob("bundle*"))


def test_published_records_never_point_at_local_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    folder = bundle.bundle_dir()
    folder.mkdir(parents=True)
    record = {"id": "kenney:k/a", "source": "kenney", "thumbnail_url": "file:///etc/x.png",
              "downloads": {"kit_zip": "/home/a/kit.zip", "converted": "/etc/passwd", "member": "a.glb"},
              "attributes": {"thumbnail": "rendered"}}
    import gzip

    with gzip.open(folder / bundle.RECORDS, "wt") as handle:
        handle.write(json.dumps(record) + "\n")
    (loaded,) = bundle.load_records()
    assert loaded["downloads"] == {"kit_page": None, "member": "a.glb"} and loaded["thumbnail_url"] is None


def test_bad_megascans_rows_are_skipped():
    good = {"source": "megascans", "kind": "model", "source_id": "abc", "title": "  "}
    assert megascans.record(good)["title"] == "abc"  # no title: fall back to the id, not a failed build
    assert megascans.record({"source": "megascans", "kind": "model"}) is None


def test_damaged_fab_cache_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    cache = view._fab_cache()
    cache.parent.mkdir(parents=True)
    cache.write_text('{"https://www.fab.com/listings/a": "https://med')  # an interrupted write
    page = '<meta property="og:image" content="https://media.fab.com/g/1/full.jpg">'
    assert view.fab_preview_url("https://www.fab.com/listings/b", page) == "https://media.fab.com/g/1/full.jpg"
    assert json.loads(cache.read_text()) == {"https://www.fab.com/listings/b": "https://media.fab.com/g/1/full.jpg"}


def test_size_keeps_its_axes():
    assert size_m([None, 1.8, None]) == [None, 1.8]  # a height alone is not a width
    assert size_m([2.0, 1.0, None]) == [2.0, 1.0]   # materials: [width, height]
    assert size_m([None, None, None]) is None


def test_outdated_data_is_refreshed_once_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    index.build([], tmp_path / "home" / "index.sqlite")
    old = bundle.pack(tmp_path / "old", version="2026.01.01", log=lambda m: None)
    new = bundle.pack(tmp_path / "new", version="2026.11.08", log=lambda m: None)
    bundle.install(old["path"], log=lambda m: None)
    monkeypatch.setattr(bundle, "DATA_VERSION", "2026.11.08")
    real_install, calls = bundle.install, []
    monkeypatch.setattr(bundle, "install", lambda *a, **k: calls.append(1) or real_install(new["path"], log=k["log"]))
    monkeypatch.setattr(server, "_checked_data", False)
    assert "2026.11.08" in server._ensure_index()
    assert server._ensure_index() is None and len(calls) == 1
    conn = sqlite3.connect(tmp_path / "home" / "index.sqlite")
    assert conn.execute("SELECT count(*) FROM assets").fetchone() == (0,)


def test_kit_licence_comes_from_its_licence_file(tmp_path):
    from sozai.catalog import LIBRARIES, kit_licence

    library = LIBRARIES["quaternius"]
    cases = {"cc0.zip": ("License.txt", "Creative Commons Zero, CC0"), "own.zip": ("LICENSE.txt", "No redistribution."),
             "none.zip": ("readme.txt", "hi")}
    seen = {}
    for name, (member, text) in cases.items():
        with zipfile.ZipFile(tmp_path / name, "w") as zf:
            zf.writestr(f"Pack/{member}", text)
        with zipfile.ZipFile(tmp_path / name) as zf:
            seen[name] = kit_licence(zf, zf.namelist(), library, "Pack")[0]
    assert seen == {"cc0.zip": "CC0-1.0", "own.zip": "LicenseRef-kit-licence", "none.zip": "CC0-1.0"}


def test_data_zip_carries_a_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    index.build([], tmp_path / "home" / "index.sqlite")
    packed = bundle.pack(tmp_path / "dist", version="2026.01.01", log=lambda m: None)
    with zipfile.ZipFile(packed["path"]) as zf:
        assert "Fab Standard License" in zf.read(bundle.NOTICE).decode()
    bundle.install(packed["path"], log=lambda m: None)
    assert (bundle.bundle_dir() / bundle.NOTICE).is_file()


def test_data_lock_is_reentrant_and_excludes_other_processes(tmp_path, monkeypatch):
    import subprocess
    from sozai import paths

    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    other = [sys.executable, "-c", "from sozai import paths\n"
             "try:\n    with paths.data_lock(timeout=0.5): print('got')\nexcept RuntimeError: print('busy')"]
    with paths.data_lock():
        with paths.data_lock():  # the same process may take it again (install inside build)
            pass
        busy = subprocess.run(other, capture_output=True, text=True, env={**__import__("os").environ})
        assert busy.stdout.strip() == "busy"
    free = subprocess.run(other, capture_output=True, text=True, env={**__import__("os").environ})
    assert free.stdout.strip() == "got"


def test_searches_close_their_index_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    index.build([], tmp_path / "home" / "index.sqlite")
    opened = []
    real = index.connect

    def tracking(path):
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(index, "connect", tracking)
    monkeypatch.setattr(server, "_checked_data", True)
    server.search_assets("crate")
    with pytest.raises(server.ToolError):
        server.get_asset("nope:x")
    assert len(opened) == 2
    for conn in opened:  # closed: Windows could not replace index.sqlite otherwise
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
