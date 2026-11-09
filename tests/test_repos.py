"""Libraries on GitHub (KayKit, Polygonal Mind, Open Source Avatars, NASA): reading, measuring, variants and fetching, offline."""
import json
import struct
from urllib.parse import unquote

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
                data = files[unquote(url[len(prefix):])]
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


AVATAR = "https://arweave.net/" + "a" * 43
VOXEL = "https://arweave.net/" + "b" * 43


def test_open_source_avatars_reads_arweave_and_skips_ipfs(monkeypatch, tmp_path):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    projects = [{"id": "100avatars-r1", "name": "100Avatars R1", "license": "CC0", "is_public": True,
                 "creator_id": "Polygonal-Mind", "avatar_data_file": "avatars/r1.json"},
                {"id": "nft", "name": "Some NFT", "license": "CC-BY", "is_public": True,
                 "creator_id": "x", "avatar_data_file": "avatars/nft.json"}]
    avatars = [{"id": "uuid-devil", "name": "Devil", "format": "VRM", "is_public": True, "description": "Avatar 031: Devil",
                "model_file_url": AVATAR, "thumbnail_url": "https://arweave.net/" + "c" * 43,
                "metadata": {"number": "031", "alternateModels": {
                    "fbx": "https://arweave.net/" + "d" * 43, "voxel_vrm": VOXEL,
                    "voxel_fbx": "https://arweave.net/" + "e" * 43}}},
               {"id": "uuid-ipfs", "name": "Gone", "format": "VRM", "is_public": True,
                "model_file_url": "https://dweb.link/ipfs/Qm/gone.vrm"}]
    model = glb(cube(), bytes(168) + png(4, 4))
    served = {repos.raw_url(repos.OSA_REGISTRY, SHA, "data/projects.json"): json.dumps(projects).encode(),
              repos.raw_url(repos.OSA_REGISTRY, SHA, "data/avatars/r1.json"): json.dumps(avatars).encode(),
              # measuring reads arweave.net/raw/<id>, the endpoint that honours Range
              "https://arweave.net/raw/" + "a" * 43: model, "https://arweave.net/raw/" + "b" * 43: model}
    accepts = []

    def get(self, url, headers=None):
        if url == f"{repos.API}/{repos.OSA_REGISTRY}/commits/HEAD":
            return json.dumps({"sha": SHA}).encode()
        assert url in served, url
        accepts.append((headers or {}).get("Accept"))
        data = served[url]
        if headers and "Range" in headers:
            start, end = headers["Range"].removeprefix("bytes=").split("-")
            return data[int(start):int(end) + 1]
        return data

    monkeypatch.setattr(Http, "get", get)
    logs = []
    report = repos.ingest("opensourceavatars", log=logs.append, thumbnails=False)
    assert report["models"] == 2  # the avatar and its voxel version; the CC-BY pack and the IPFS one left out
    assert any("CC-BY" in line for line in logs) and any("IPFS" in line for line in logs)
    assert "application/json" not in accepts  # Arweave would answer with JSON about the file
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["opensourceavatars"]}
    devil, voxel = rows["100avatars-r1/031_Devil"], rows["100avatars-r1/031_Devil_Voxel"]
    assert devil["title"] == "Devil" and voxel["title"] == "Devil (voxel)"
    assert devil["downloads"]["files"] == {"031_Devil.vrm": AVATAR} and devil["attributes"]["format"] == "vrm"
    assert devil["attributes"]["alternates"] == {"fbx": "https://arweave.net/" + "d" * 43}
    assert voxel["attributes"]["alternates"] == {"fbx": "https://arweave.net/" + "e" * 43}
    assert devil["variant_group"] == voxel["variant_group"] == "opensourceavatars:100avatars-r1/031_Devil"
    assert devil["url"] == repos.OSA_PAGE + "uuid-devil" and devil["authors"] == ["Polygonal Mind"]
    assert devil["licence"] == "CC0-1.0" and devil["style"] == "lowpoly" and devil["size_y"] == 0.5
    assert {"avatar", "character", "devil"} <= set(devil["tags"]) and "voxel" in voxel["tags"]
    assert devil["thumbnail_url"] and voxel["thumbnail_url"] is None
    assert AVATAR in how_to_get(devil) and "FBX" in how_to_get(devil)


def test_fetch_keeps_an_avatar_as_one_vrm(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    model = glb(cube(), bytes(168) + png(4, 4))

    def download(url, md5=None):
        path = tmp_path / "cache" / "x"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(model)
        return path

    monkeypatch.setattr(fetch, "download", download)
    asset = {"id": "opensourceavatars:r1/031_Devil", "title": "Devil", "source": "opensourceavatars", "source_id": "r1/031_Devil",
             "licence": "CC0-1.0", "credit": "Devil", "attributes": {},
             "downloads": {"main": "031_Devil.vrm", "files": {"031_Devil.vrm": AVATAR}}}
    out = fetch.fetch_asset(asset, str(tmp_path / "project"))
    assert out["files"] == ["031_Devil.vrm"] and any("VRM" in note for note in out["notes"])
    assert (tmp_path / "project" / "031_Devil.vrm").read_bytes() == model


NASA_PAGE = """<html><head>
<meta property="og:description" content="Aqua carries six instruments to observe the Earth&#039;s oceans" />
<meta property="og:image" content="https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/model/aqua-(a)/Aqua%20(A).png/jcr:content/renditions/cq5dam.web.1280.1280.png" />
</head><body><a href="https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/model/aqua-(a)/Aqua%20(A).glb">GLB</a></body></html>"""


def test_nasa_reads_pages_and_reports_no_size(monkeypatch, tmp_path):
    import urllib.error

    from sozai.search import compact

    model = glb(cube(), bytes(168) + png(4, 4))
    files = {"3D Models/Aqua (A)/Aqua (A).glb": model, "3D Models/Aqua (A)/Aqua (A).png": png(8, 8),
             "3D Models/Stardust/Stardust.glb": model, "3D Models/Stardust/Stardust capsule.glb": model,
             "3D Models/Stardust/Stardust capsule.png": png(8, 8),
             "3D Models/Curiosity Rover (MSL)/Curiosity Rover (MSL).blend": b"blend",
             "3D Printing/Aqua/Aqua.stl": b"stl"}
    server = FakeGitHub({repos.NASA_REPO: files})
    pages = []

    def get(self, url, headers=None):
        if url.startswith(repos.NASA_SITE):
            pages.append(url)
            if url == repos.NASA_SITE + "aqua-a/":
                return NASA_PAGE.encode()
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return server.get(url, headers)

    monkeypatch.setattr(Http, "get", get)
    monkeypatch.setattr(repos, "SITE_PAUSE", 0)
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    logs = []
    report = repos.ingest("nasa", log=logs.append, thumbnails=False)
    assert report["models"] == 3 and sorted(pages) == [repos.NASA_SITE + "aqua-a/", repos.NASA_SITE + "stardust/"]
    assert any("Curiosity Rover (MSL)" in line for line in logs)  # .blend only: left out, and said so
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["nasa"]}
    assert sorted(rows) == ["models/Aqua_A", "models/Stardust", "models/Stardust_capsule"]
    aqua = rows["models/Aqua_A"]
    assert aqua["title"] == "Aqua (A)" and aqua["url"] == repos.NASA_SITE + "aqua-a/"
    assert aqua["description"] == "Aqua carries six instruments to observe the Earth's oceans."
    assert aqua["thumbnail_url"] == ("https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/model/"
                                     "aqua-(a)/Aqua%20(A).png" + repos.NASA_THUMB)
    # NASA files share no unit: the size stays in the file's units and none is reported in metres
    assert aqua["size_x"] is None and aqua["attributes"]["size_units"] == [0.5, 0.5, 0.5]
    assert aqua["licence"] == repos.NASA_LICENCE and aqua["redistributable"] and "endorsement" in aqua["credit"]
    assert "nasa" in aqua["tags"] and compact(aqua)["licence"] == repos.NASA_LICENCE
    capsule, stardust = rows["models/Stardust_capsule"], rows["models/Stardust"]
    assert capsule["title"] == "Stardust capsule" and capsule["url"].startswith("https://github.com/nasa/")
    assert capsule["thumbnail_url"] == repos.raw_url(repos.NASA_REPO, SHA, "3D Models/Stardust/Stardust capsule.png")
    assert stardust["thumbnail_url"] is None  # rendered with Blender at ingest time
    line = how_to_get(aqua)
    assert "fetch_asset" in line and "endorsement" in line and "real object" in line
    assert repos._nasa_slug("Active Cavity Irradiance Monitor Satellite (AcrimSAT) (A)") == \
        "active-cavity-irradiance-monitor-satellite-acrimsat-a"
    assert repos._nasa_slug("70-meter Dish") == "70-meter-dish"

    def download(url, md5=None):
        path = tmp_path / "cache" / url.rsplit("/", 1)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(server.get(url))
        return path

    monkeypatch.setattr(fetch, "download", download)
    out = fetch.fetch_asset(aqua, str(tmp_path / "project"))
    assert out["files"] == ["Aqua_A.glb"] and any("not scaled to metres" in note for note in out["notes"])
