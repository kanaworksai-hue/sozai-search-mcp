import json
import struct
import zlib

import pytest

from sozai.measure import image_size, measure


def png(width, height) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def glb(doc: dict, binary: bytes) -> bytes:
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    binary += b"\0" * (-len(binary) % 4)
    total = 12 + 8 + len(body) + 8 + len(binary)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(body), 0x4E4F534A) + body
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


def test_glb_box_follows_node_transforms(tmp_path):
    image = png(512, 256)
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        # parent scales by 2 and lifts by 1; child moves 1 along x and holds a unit cube mesh
        "nodes": [{"scale": [2, 2, 2], "translation": [0, 1, 0], "children": [1]},
                  {"translation": [1, 0, 0], "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [{"count": 8, "min": [-0.5, -0.5, -0.5], "max": [0.5, 0.5, 0.5], "type": "VEC3", "componentType": 5126},
                      {"count": 36, "type": "SCALAR", "componentType": 5123}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(image)}],
        "buffers": [{"byteLength": len(image)}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
        "skins": [{"joints": [1]}],
        "animations": [{"name": "Walk", "channels": [], "samplers": []}, {"channels": [], "samplers": []}],
        "materials": [{}],
    }
    path = tmp_path / "cube.glb"
    path.write_bytes(glb(doc, image))
    result = measure(path)
    assert result["size"] == [2.0, 2.0, 2.0]
    assert result["triangles"] == 12 and result["vertices"] == 8
    assert result["rigged"] and result["animated"] and result["animations"] == ["Walk", "animation_1"]
    assert result["max_texture_px"] == 512 and result["materials"] == 1


def test_rotation_turns_the_box(tmp_path):
    # 90 degrees about z: a 2 x 1 x 1 box becomes 1 x 2 x 1
    s = 2 ** -0.5
    doc = {"asset": {"version": "2.0"}, "nodes": [{"rotation": [0, 0, s, s], "mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"count": 3, "min": [-1, -0.5, -0.5], "max": [1, 0.5, 0.5]}]}
    path = tmp_path / "bar.gltf"
    path.write_text(json.dumps(doc))
    result = measure(path)
    assert result["size"] == pytest.approx([1.0, 2.0, 1.0], abs=1e-3)
    assert result["triangles"] == 1 and not result["rigged"]


def test_obj(tmp_path):
    path = tmp_path / "quad.obj"
    path.write_text("v 0 0 0\nv 2 0 0\nv 2 3 0\nv 0 3 1\nf 1 2 3 4\n")
    result = measure(path)
    assert result["size"] == [2.0, 3.0, 1.0] and result["triangles"] == 2


def test_image_sizes():
    assert image_size(png(64, 32)) == (64, 32)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 600, 800) + b"\0" * 8
    assert image_size(jpeg) == (800, 600)
    assert image_size(b"nothing") is None
