"""Kit ingest, glTF packing and fetch_asset, offline (downloads are served from local files)."""
import json
import zipfile

import pytest

from sozai import catalog, fetch, index
from sozai.build import build_index
from sozai.gltf_pack import pack
from sozai.measure import measure, read_gltf
from sozai.search import Searcher

from test_measure import glb, png

CUBE_DOC = {
    "asset": {"version": "2.0"}, "nodes": [{"mesh": 0}],
    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
    "accessors": [{"count": 8, "min": [-0.25, 0, -0.25], "max": [0.25, 0.5, 0.25], "type": "VEC3", "componentType": 5126, "bufferView": 0},
                  {"count": 36, "type": "SCALAR", "componentType": 5123, "bufferView": 1}],
    "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 96}, {"buffer": 0, "byteOffset": 96, "byteLength": 72}],
    "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
    "textures": [{"source": 0}],
}


def write_gltf(folder, name="crate"):
    doc = json.loads(json.dumps(CUBE_DOC))
    doc["buffers"] = [{"uri": f"{name}.bin", "byteLength": 168}]
    doc["images"] = [{"uri": f"textures/{name}_diff.png"}]
    (folder / "textures").mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.bin").write_bytes(bytes(range(168)))
    (folder / "textures" / f"{name}_diff.png").write_bytes(png(256, 128))
    path = folder / f"{name}.gltf"
    path.write_text(json.dumps(doc))
    return path


def test_pack_embeds_buffers_and_images(tmp_path):
    out = pack(write_gltf(tmp_path), tmp_path / "out" / "crate.glb")
    doc, binary = read_gltf(out)
    assert "uri" not in doc["images"][0] and doc["images"][0]["mimeType"] == "image/png"
    assert binary[:168] == bytes(range(168))
    view = doc["bufferViews"][doc["images"][0]["bufferView"]]
    assert binary[view["byteOffset"]:view["byteOffset"] + 8] == b"\x89PNG\r\n\x1a\n"
    facts = measure(out)
    assert facts["size"] == [0.5, 0.5, 0.5] and facts["triangles"] == 12 and facts["max_texture_px"] == 256


def make_kit(folder):
    """A Kenney-style kit zip: a GLB pointing at an external texture, with a preview, and a glTF with external files."""
    gltf_dir = folder / "src"
    write_gltf(gltf_dir, "barrel")
    kit = folder / "kenney_test-kit_2.1.zip"  # Kenney adds a version to some zip names
    crate = json.loads(json.dumps(CUBE_DOC)) | {"buffers": [{"byteLength": 168}], "images": [{"uri": "Textures/colormap.png"}]}
    with zipfile.ZipFile(kit, "w") as zf:
        zf.writestr("Models/GLB format/crate-wide.glb", glb(crate, bytes(168)))
        zf.writestr("Models/GLB format/Textures/colormap.png", png(32, 32))
        zf.writestr("Models/OBJ format/crate-wide.obj", "v 0 0 0\nv 1 1 1\nf 1 2 2\n")  # same model, lower preference
        zf.writestr("Previews/crate-wide.png", png(64, 64))
        for file in ("barrel.gltf", "barrel.bin", "textures/barrel_diff.png"):
            zf.write(gltf_dir / file, f"Models/glTF format/{file}")
        zf.writestr("License.txt", "CC0")
    return kit


@pytest.fixture
def kit_index(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    kits = tmp_path / "kits"
    kits.mkdir()
    make_kit(kits)
    report = catalog.ingest("kenney", kits, log=lambda m: None)
    build_index(only=["kenney"], log=lambda m: None, live=True)
    return report


def test_ingest_measures_each_model_once(kit_index, tmp_path):
    assert kit_index["models"] == 2 and kit_index["uncalibrated_kits"] == ["test-kit"]
    searcher = Searcher(index.connect(tmp_path / "home" / "index.sqlite"))
    crate = searcher.get("kenney:test-kit/crate-wide")
    # no reference object for this kit: no metre sizes, the file units kept aside
    assert crate["title"] == "Crate Wide" and crate["size_m"] is None and crate["polycount"] == 12
    assert crate["attributes"]["size_units"] == [0.5, 0.5, 0.5]
    assert crate["attributes"]["format"] == "glb" and crate["attributes"]["collection"] == "Test Kit"
    assert crate["max_texture_px"] == 32  # the external texture was extracted next to the model and read
    assert crate["url"] == "https://kenney.nl/assets/test-kit"  # version suffix stripped from the zip name
    assert crate["thumbnail_url"].startswith("file://") and crate["acquire"] == "local"
    assert "fetch_asset" in crate["how_to_get"]
    found = searcher.search("barrel", source="kenney")["results"]
    assert [r["id"] for r in found] == ["kenney:test-kit/barrel"]


def test_quaternius_kit_split_over_zips_keeps_best_format(tmp_path, monkeypatch):
    from sozai import convert

    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(convert, "find_blender", lambda: None)
    kits = tmp_path / "kits"
    kits.mkdir()
    with zipfile.ZipFile(kits / "quaternius_animatedalien.zip", "w") as zf:
        zf.writestr("OBJ/Alien.obj", "v 0 0 0\nv 1 2 1\nv 1 0 0\nf 1 2 3\n")
        zf.writestr("OBJ/Rock.obj", "v 0 0 0\nv 1 1 1\nv 1 0 0\nf 1 2 3\n")
        zf.writestr("OBJ/Empty.obj", "v 0 0 0\n")  # no faces: left out
    with zipfile.ZipFile(kits / "quaternius_animatedalien_gltf.zip", "w") as zf:
        zf.writestr("glTF/Alien.glb", glb(json.loads(json.dumps(CUBE_DOC)) | {"buffers": [{"byteLength": 168}]}, bytes(168)))
        zf.writestr("FBX/Spaceship.fbx", b"Kaydara FBX Binary")  # Blender missing: skipped, not fatal
    logs = []
    report = catalog.ingest("quaternius", kits, log=logs.append)
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["quaternius"]}
    assert set(rows) == {"animatedalien/Alien", "animatedalien/Rock"} and report["models"] == 2
    assert rows["animatedalien/Alien"]["attributes"]["format"] == "glb"  # the GLB beat the OBJ
    assert rows["animatedalien/Alien"]["url"] == "https://quaternius.com/packs/animatedalien.html"
    assert any("Blender not found" in line for line in logs)


def test_calibrated_kit_is_scaled_to_metres(tmp_path, monkeypatch):
    from sozai import scale

    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(scale.KENNEY, "test-kit", ("crate-*", "height", 1.0, "a crate is 1 m tall"))
    kits = tmp_path / "kits"
    kits.mkdir()
    make_kit(kits)
    report = catalog.ingest("kenney", kits, log=lambda m: None)
    assert report["calibrated_kits"] == 1 and report["uncalibrated_kits"] == []
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["kenney"]}
    crate, barrel = rows["test-kit/crate-wide"], rows["test-kit/barrel"]
    assert [crate["size_x"], crate["size_y"], crate["size_z"]] == [1.0, 1.0, 1.0]  # 0.5 units * 2
    assert barrel["size_y"] == 1.0 and crate["attributes"]["scale"]["factor"] == 2.0
    assert crate["size_source"] == "measured, calibrated"
    # the fetched file itself arrives at real size, not just the index entry
    build_index(only=["kenney"], log=lambda m: None, live=True)
    searcher = Searcher(index.connect(tmp_path / "home" / "index.sqlite"))
    out = fetch.fetch_asset(searcher.get("kenney:test-kit/crate-wide"), str(tmp_path / "out"))
    assert measure(out["main_file"])["size"] == [1.0, 1.0, 1.0]
    assert any("scaled by 2.0" in note for note in out["notes"])
    split = fetch.fetch_asset(searcher.get("kenney:test-kit/barrel"), str(tmp_path / "out2"), fmt="gltf")
    assert measure(split["main_file"])["size"] == [1.0, 1.0, 1.0]


def test_fetch_from_kit(kit_index, tmp_path):
    searcher = Searcher(index.connect(tmp_path / "home" / "index.sqlite"))
    dest = tmp_path / "project" / "assets"
    out = fetch.fetch_asset(searcher.get("kenney:test-kit/barrel"), str(dest))
    assert out["main_file"].endswith("test-kit_barrel.glb") and out["files"] == ["test-kit_barrel.glb"]
    assert measure(dest / "test-kit_barrel.glb")["max_texture_px"] == 256
    glb_out = fetch.fetch_asset(searcher.get("kenney:test-kit/crate-wide"), str(dest))
    assert glb_out["files"] == ["test-kit_crate-wide.glb"]
    doc, _ = read_gltf(dest / "test-kit_crate-wide.glb")
    assert "uri" not in doc["images"][0]  # the external texture is now inside the .glb
    assert measure(dest / "test-kit_crate-wide.glb")["max_texture_px"] == 32
    credits = (dest / "CREDITS.md").read_text()
    assert credits.count("Test Kit by Kenney") == 2


def test_fetch_kit_missing_says_how_to_get_it(kit_index, tmp_path):
    for zip_path in (tmp_path / "kits").glob("*.zip"):
        zip_path.unlink()
    rows = catalog.load_catalogs()["kenney"]
    assert {r["acquire"] for r in rows} == {"human"}
    with pytest.raises(fetch.ToolError, match="sozai ingest kenney"):
        fetch.fetch_asset(dict(rows[0], size_m=None), str(tmp_path / "out"))


def test_fetch_ambientcg_zip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    archive = tmp_path / "Bricks001_1K-JPG.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("Bricks001_1K-JPG_Color.jpg", "Bricks001_1K-JPG.tres", "../evil.txt"):
            zf.writestr(name, "x")
    monkeypatch.setattr(fetch, "download", lambda url, md5=None: archive)
    asset = {"id": "ambientcg:Bricks001", "source": "ambientcg", "source_id": "Bricks001", "title": "Bricks 001",
             "kind": "material", "licence": "CC0-1.0", "credit": "credit", "url": "u", "size_m": [1, 1],
             "downloads": {"zips": [{"attributes": "1K-JPG", "url": "https://ambientcg.com/get?file=Bricks001_1K-JPG.zip"}]}}
    with pytest.raises(fetch.ToolError, match="unsafe path"):
        fetch.fetch_asset(asset, str(tmp_path / "out"), resolution="1k")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Bricks001_1K-JPG_Color.jpg", "x")
        zf.writestr("Bricks001_1K-JPG.tres", "x")
    out = fetch.fetch_asset(asset, str(tmp_path / "out"), resolution="1k")
    assert out["main_file"].endswith(".tres") and len(out["files"]) == 2


def test_fetch_polyhaven_model_and_resolution_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    src = tmp_path / "src"
    write_gltf(src, "crate_1k")
    served = {"crate_1k.gltf": src / "crate_1k.gltf", "crate_1k.bin": src / "crate_1k.bin",
              "crate_1k_diff.png": src / "textures" / "crate_1k_diff.png"}
    files_json = {"gltf": {"1k": {"gltf": {"url": "https://dl.test/crate_1k.gltf", "include": {
        "crate_1k.bin": {"url": "https://dl.test/crate_1k.bin"},
        "textures/crate_1k_diff.png": {"url": "https://dl.test/crate_1k_diff.png"}}}}}}
    monkeypatch.setattr(fetch.Http, "get_json", lambda self, url: files_json)
    monkeypatch.setattr(fetch, "download", lambda url, md5=None: served[url.rsplit("/", 1)[1]])
    asset = {"id": "polyhaven:crate", "source": "polyhaven", "source_id": "crate", "title": "Crate", "kind": "model",
             "licence": "CC0-1.0", "credit": "Powered by Poly Haven", "url": "u", "size_m": [0.5, 0.5, 0.5],
             "downloads": {"files_api": "https://api.polyhaven.com/files/crate"}}
    out = fetch.fetch_asset(asset, str(tmp_path / "out"), quality="film")  # asks 4k, only 1k exists
    assert out["files"] == ["crate_1k.glb"] and "used 1k" in out["notes"][0]
    assert measure(tmp_path / "out" / "crate_1k.glb")["triangles"] == 12
    split = fetch.fetch_asset(asset, str(tmp_path / "out2"), resolution="1k", fmt="gltf")
    assert sorted(split["files"]) == ["crate_1k/crate_1k.bin", "crate_1k/crate_1k.gltf", "crate_1k/textures/crate_1k_diff.png"]
