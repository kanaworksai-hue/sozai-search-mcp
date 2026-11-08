"""Rendered previews for kit models without one, and textures kept in a shared kit folder."""
import json
import struct
import zipfile

import pytest

from conftest import REAL_FIND_BLENDER
from sozai import catalog, convert, fetch, index, thumbs
from sozai.build import build_index
from sozai.measure import measure, read_gltf
from sozai.search import Searcher

from test_fetch import CUBE_DOC, make_kit
from test_measure import glb, png


def shared_texture_kit(folder):
    """A Quaternius-style kit: models point at textures beside themselves, the kit keeps them in Textures/."""
    doc = json.loads(json.dumps(CUBE_DOC)) | {"buffers": [{"uri": "Prop.bin", "byteLength": 168}],
                                               "images": [{"uri": "T_Trim_BaseColor.png"}]}
    kit = folder / "quaternius_scifikit.zip"
    with zipfile.ZipFile(kit, "w") as zf:
        zf.writestr("SciFi Kit/glTF/Props/Prop.gltf", json.dumps(doc))
        zf.writestr("SciFi Kit/glTF/Props/Prop.bin", bytes(168))
        zf.writestr("SciFi Kit/Textures/T_Trim_BaseColor.png", png(64, 64))
        zf.writestr("Other/T_Trim_BaseColor.png", png(8, 8))  # same name, farther away: not chosen
        broken = json.loads(json.dumps(doc)) | {"buffers": [{"uri": "Gone_001.bin", "byteLength": 168}]}
        zf.writestr("SciFi Kit/glTF/Props/Broken.gltf", json.dumps(broken))  # its mesh data is missing
    return kit


def test_texture_in_shared_folder_is_found_and_packed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    kits = tmp_path / "kits"
    kits.mkdir()
    shared_texture_kit(kits)
    logs = []
    report = catalog.ingest("quaternius", kits, log=logs.append)
    assert report["models"] == 1 and any("lacks Gone_001.bin" in line for line in logs)
    build_index(only=["quaternius"], log=lambda m: None, live=True)
    prop = Searcher(index.connect(tmp_path / "home" / "index.sqlite")).get("quaternius:scifikit/Prop")
    assert prop["downloads"]["moved"] == {"SciFi Kit/Textures/T_Trim_BaseColor.png":
                                          "SciFi Kit/glTF/Props/T_Trim_BaseColor.png"}
    assert prop["max_texture_px"] == 64
    out = fetch.fetch_asset(prop, str(tmp_path / "out"))
    doc, _ = read_gltf(out["main_file"])
    assert "uri" not in doc["images"][0] and measure(out["main_file"])["max_texture_px"] == 64
    split = fetch.fetch_asset(prop, str(tmp_path / "split"), fmt="gltf")
    assert (tmp_path / "split" / "scifikit_Prop" / "SciFi Kit/glTF/Props/T_Trim_BaseColor.png").is_file()
    assert split["files"][-1].endswith("glTF/Props/T_Trim_BaseColor.png")


def test_render_missing_reuses_earlier_renders_and_reports_without_blender(tmp_path):
    previews = tmp_path / "previews"
    kit = make_kit(tmp_path)
    rows = [{"source": "kenney", "source_id": f"test-kit/{name}", "thumbnail_url": None, "attributes": {},
             "downloads": {"kit_zip": str(kit), "member": "Models/glTF format/barrel.gltf"}}
            for name in ("barrel", "crate")]
    rows.append({"source": "kenney", "source_id": "test-kit/own", "thumbnail_url": "file:///kit-preview.png",
                 "attributes": {}, "downloads": {}})
    done = thumbs.rendered_path(previews, rows[0])
    done.parent.mkdir(parents=True)
    done.write_bytes(png(4, 4))
    logs = []
    report = thumbs.render_missing(rows, previews, logs.append)
    assert report == {"rendered": 0, "reused": 1, "failed": 1}
    assert rows[0]["thumbnail_url"] == done.resolve().as_uri() and rows[0]["attributes"]["thumbnail"] == "rendered"
    assert rows[1]["thumbnail_url"] is None and "install Blender" in logs[0]
    assert rows[2]["thumbnail_url"] == "file:///kit-preview.png"  # the kit's own preview is kept


def cube_bin() -> bytes:
    """Real geometry for CUBE_DOC's buffer: 8 corners and 12 triangles (Blender rejects random bytes)."""
    corners = [(x, y, z) for x in (-0.25, 0.25) for y in (0, 0.5) for z in (-0.25, 0.25)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return struct.pack("<24f", *[c for p in corners for c in p]) + struct.pack("<36H", *[i for f in faces for i in f])


@pytest.mark.skipif(REAL_FIND_BLENDER() is None, reason="Blender is not installed")
def test_ingest_renders_previews_with_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "find_blender", REAL_FIND_BLENDER)
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    kits = tmp_path / "kits"
    kits.mkdir()
    cube = json.loads(json.dumps(CUBE_DOC)) | {"buffers": [{"byteLength": 168}], "images": [{"uri": "colormap.png"}]}
    with zipfile.ZipFile(kits / "kenney_test-kit.zip", "w") as zf:
        zf.writestr("Models/barrel.glb", glb(cube, cube_bin()))
        zf.writestr("Models/crate.glb", glb(cube, cube_bin()))
        zf.writestr("Models/colormap.png", png(8, 8))
        zf.writestr("Previews/crate.png", png(64, 64))
    report = catalog.ingest("kenney", kits, log=lambda m: None)
    assert report["previews"] == {"rendered": 1, "reused": 0, "failed": 0}  # the crate ships a preview
    rows = {r["source_id"]: r for r in catalog.load_catalogs()["kenney"]}
    assert rows["test-kit/barrel"]["attributes"]["thumbnail"] == "rendered"
    assert "thumbnail" not in rows["test-kit/crate"]["attributes"]
    data = (tmp_path / "home" / "catalog" / "previews" / "kenney" / "test-kit" / "rendered" / "barrel.webp").read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) < 30_000
    again = catalog.ingest("kenney", kits, log=lambda m: None)
    assert again["previews"] == {"rendered": 0, "reused": 1, "failed": 0}
