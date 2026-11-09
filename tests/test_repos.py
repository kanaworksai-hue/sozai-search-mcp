"""Libraries on GitHub (KayKit, Polygonal Mind): reading, measuring, variants and fetching, offline."""
import json
import struct

import pytest
from test_fetch import CUBE_DOC
from test_measure import glb, png

from sozai import catalog, fetch, repos
from sozai.http import Http
from sozai.measure import measure, read_gltf
from sozai.search import how_to_get

KK = "KayKit-Game-Assets/KayKit-Furniture-Bits-1.0"
SHA = "c0ffee"
GLTF = "addons/kk/Assets/gltf"


def cube(buffer_uri=None, image_uri=None) -> dict:
    doc = json.loads(json.dumps(CUBE_DOC))
    doc["buffers"] = [{"byteLength": 168, **({"uri": buffer_uri} if buffer_uri else {})}]
    doc["images"] = [{"uri": image_uri} if image_uri else {"bufferView": 0, "mimeType": "image/png"}]
    return doc


class FakeGitHub:
    """Serves the GitHub API and raw files from dicts, honouring Range like raw.githubusercontent.com."""

    def __init__(self, repos_files: dict[str, dict[str, bytes]]):
        self.files = repos_files
        self.ranges: list[str] = []

    def get(self, url: str, headers: dict | None = None) -> bytes:
        for repo, files in self.files.items():
            if url == f"{repos.API}/{repo}/commits/HEAD":
                return json.dumps({"sha": SHA}).encode()
            if url == f"{repos.API}/{repo}/git/trees/{SHA}?recursive=1":
                return json.dumps({"tree": [{"path": p, "type": "blob"} for p in files]}).encode()
            prefix = f"{repos.RAW}/{repo}/{SHA}/"
            if url.startswith(prefix):
                data = files[url[len(prefix):].replace("%20", " ")]
                if headers and "Range" in headers:
                    self.ranges.append(headers["Range"])
                    start, end = headers["Range"].removeprefix("bytes=").split("-")
                    return data[int(start):int(end) + 1]
                return data
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture
def kaykit_repo(monkeypatch):
    files = {
        f"{GLTF}/table_medium.gltf": json.dumps(cube("table_medium.bin", "furniture_texture.png")).encode(),
        f"{GLTF}/table_medium.bin": bytes(168),
        f"{GLTF}/chair_blue.gltf": json.dumps(cube("chair.bin", "furniture_texture.png")).encode(),
        f"{GLTF}/chair_red.gltf": json.dumps(cube("chair.bin", "furniture_texture.png")).encode(),
        f"{GLTF}/chair.bin": bytes(168),
        f"{GLTF}/broken.gltf": json.dumps(cube("missing.bin")).encode(),
        "addons/kk/Assets/texture/furniture_texture.png": png(8, 8),
        "addons/kk/Assets/fbx/table_medium.fbx": b"fbx",
        "addons/kk/Samples/sample.gltf": json.dumps(cube()).encode(),
    }
    server = FakeGitHub({KK: files})
    monkeypatch.setattr(repos, "KAYKIT_REPOS", [KK])
    monkeypatch.setattr(Http, "get", lambda self, url, headers=None: server.get(url, headers))
    return server


def test_kaykit_records_are_measured_pinned_and_calibrated(kaykit_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    logs = []
    report = repos.ingest("kaykit", log=logs.append)
    assert report["models"] == 3 and report["calibrated_packs"] == 1  # broken, fbx and sample left out
    assert any("broken.gltf" in line for line in logs)
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["kaykit"]}
    table = rows["furniture-bits/table_medium"]
    # the reference table is 0.5 units tall and a table is 0.75 m: every model of the pack grows by 1.5
    assert table["size_y"] == 0.75 and table["attributes"]["scale"]["factor"] == 1.5
    assert table["licence"] == "CC0-1.0" and table["redistributable"] and table["acquire"] == "direct"
    assert table["attributes"]["collection"] == "KayKit Furniture Bits" and "Kay Lousberg" in table["credit"]
    files = table["downloads"]["files"]
    # the texture the model expects beside it lives in another folder of the repository
    assert files[f"{GLTF}/furniture_texture.png"] == f"{repos.RAW}/{KK}/{SHA}/addons/kk/Assets/texture/furniture_texture.png"
    assert all(f"/{SHA}/" in url for url in files.values())
    chairs = [rows["furniture-bits/chair_blue"], rows["furniture-bits/chair_red"]]
    assert chairs[0]["variant_group"] == chairs[1]["variant_group"] == "kaykit:furniture-bits/chair"
    assert table["variant_group"] is None and {"chair", "blue"} <= set(chairs[0]["tags"])
    assert "fetch_asset" in how_to_get(table) and "furniture_texture.png" in how_to_get(table)


def test_fetch_from_github_packs_one_glb_at_real_size(kaykit_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    repos.ingest("kaykit", log=lambda m: None)
    table = {r["source_id"]: r for r in catalog.load_catalogs()["kaykit"]}["furniture-bits/table_medium"]
    served = tmp_path / "served"

    def download(url, md5=None):
        path = served / url.rsplit("/", 1)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(kaykit_repo.get(url))
        return path

    monkeypatch.setattr(fetch, "download", download)
    out = fetch.fetch_asset(table, str(tmp_path / "project"))
    main = tmp_path / "project" / "table_medium.glb"
    assert out["main_file"] == str(main) and out["files"] == ["table_medium.glb"]
    doc, _ = read_gltf(main)
    assert not any("uri" in item for item in doc["buffers"] + doc["images"])  # self-contained
    assert measure(main)["size"][1] == pytest.approx(0.75)
    assert any("scaled by 1.5" in note for note in out["notes"])
    kept = fetch.fetch_asset(table, str(tmp_path / "gltf"), fmt="gltf")
    assert sorted(kept["files"]) == ["table_medium/furniture_texture.png", "table_medium/table_medium.bin",
                                     "table_medium/table_medium.gltf"]


def test_glb_is_measured_from_its_json_chunk_only(monkeypatch):
    data = glb(cube(), bytes(168) + png(4, 4) + bytes(5000))
    server = FakeGitHub({"o/r": {"m.glb": data}})
    monkeypatch.setattr(repos, "HEAD_BYTES", 32)  # the JSON chunk does not fit: a second range read
    doc = repos.gltf_json(type("H", (), {"get": lambda self, url, headers=None: server.get(url, headers)})(),
                          repos.raw_url("o/r", SHA, "m.glb"))
    length = struct.unpack("<I", data[12:16])[0]
    assert doc["accessors"][0]["max"] == [0.25, 0.5, 0.25]
    assert server.ranges == ["bytes=0-31", f"bytes=32-{19 + length}"]


def test_polygonal_mind_reads_the_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    raw = f"{repos.RAW}/{repos.PM_MODELS}/main/projects/fair"
    projects = [{"id": "pm-medieval-fair", "name": "medieval-fair", "license": "CC0", "is_public": True,
                 "github_url": "https://github.com/x/fair", "asset_data_file": "assets/pm-medieval-fair.json"},
                {"id": "pm-secret", "name": "secret", "license": "CC0", "is_public": False,
                 "asset_data_file": "assets/pm-secret.json"}]
    assets = [{"name": "Balloon_Red", "model_file_url": f"{raw}/Balloon_Red.glb", "is_public": True,
               "thumbnail_url": f"{raw}/Balloon_Red_thumbnail.png",
               "metadata": {"attributes": [{"trait_type": "Theme", "value": "Medieval"},
                                           {"trait_type": "Type", "value": "Balloon"}]}},
              {"name": "Balloon_Blue", "model_file_url": f"{raw}/Balloon_Blue.glb", "is_public": True},
              {"name": "Gone", "model_file_url": f"{raw}/Gone.glb", "is_public": True},
              {"name": "Draft", "model_file_url": f"{raw}/Balloon_Red.glb", "is_draft": True}]
    model = glb(cube(), bytes(168) + png(4, 4))
    server = FakeGitHub({
        repos.PM_REGISTRY: {"data/projects.json": json.dumps(projects).encode(),
                            "data/assets/pm-medieval-fair.json": json.dumps(assets).encode()},
        repos.PM_MODELS: {"projects/fair/Balloon_Red.glb": model, "projects/fair/Balloon_Blue.glb": model,
                          "projects/fair/Balloon_Red_thumbnail.png": png(4, 4)},
    })
    monkeypatch.setattr(Http, "get", lambda self, url, headers=None: server.get(url, headers))
    report = repos.ingest("polygonalmind", log=lambda m: None, thumbnails=False)
    assert report["models"] == 2 and report["calibrated_packs"] == 0
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["polygonalmind"]}
    red = rows["medieval-fair/Balloon_Red"]
    assert red["size_y"] == 0.5 and "scale" not in red["attributes"]  # trusted to be in metres
    assert red["thumbnail_url"] == f"{repos.RAW}/{repos.PM_MODELS}/{SHA}/projects/fair/Balloon_Red_thumbnail.png"
    assert rows["medieval-fair/Balloon_Blue"]["thumbnail_url"] is None
    assert red["attributes"]["traits"] == {"theme": "Medieval", "type": "Balloon"}
    assert {"balloon", "red", "medieval"} <= set(red["tags"]) and red["url"] == "https://github.com/x/fair"
    assert red["variant_group"] == rows["medieval-fair/Balloon_Blue"]["variant_group"]
    assert red["downloads"]["files"] == {"projects/fair/Balloon_Red.glb":
                                         f"{repos.RAW}/{repos.PM_MODELS}/{SHA}/projects/fair/Balloon_Red.glb"}


def test_rate_limited_api_falls_back_to_git(monkeypatch):
    import urllib.error

    def limited(self, url, headers=None):
        raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)

    calls = []

    def git(*args):
        calls.append(args[0])
        return {"ls-remote": f"{SHA}\tHEAD\n", "clone": "", "-C": "a.gltf\nb/c.bin\n"}[args[0]]

    monkeypatch.setattr(Http, "get", limited)
    monkeypatch.setattr(repos, "_git", git)
    monkeypatch.setattr(repos.shutil, "which", lambda name: "/usr/bin/git")
    assert repos._commit(Http(), "o/r") == SHA
    assert repos._tree(Http(), "o/r", SHA) == ["a.gltf", "b/c.bin"]
    assert calls == ["ls-remote", "clone", "-C"]
