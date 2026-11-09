"""Measure 3D files without a 3D library: bounding box, triangles, rig, animations, textures.

glTF / GLB: the spec requires min/max on every POSITION accessor, so the box comes from the JSON
alone (each mesh's local box, its 8 corners moved through the node's world matrix). glTF is Y-up
and in metres. OBJ: every vertex is read; units are whatever the exporter used (usually metres).
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

GLB_MAGIC = 0x46546C67  # "glTF"
CHUNK_JSON, CHUNK_BIN = 0x4E4F534A, 0x004E4942


# --- small 4x4 matrix helpers (column-major, as glTF stores them) ---

def _identity() -> list[float]:
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _multiply(a: list[float], b: list[float]) -> list[float]:
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return out


def _trs(node: dict) -> list[float]:
    if "matrix" in node:
        return [float(v) for v in node["matrix"]]
    tx, ty, tz = node.get("translation", (0, 0, 0))
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    rot = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
           2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
           2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]
    return [rot[0] * sx, rot[1] * sx, rot[2] * sx, 0,
            rot[3] * sy, rot[4] * sy, rot[5] * sy, 0,
            rot[6] * sz, rot[7] * sz, rot[8] * sz, 0,
            tx, ty, tz, 1.0]


def _apply(m: list[float], p: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = p
    return (m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


class Box:
    def __init__(self):
        self.lo = [math.inf] * 3
        self.hi = [-math.inf] * 3

    def add(self, p) -> None:
        for i in range(3):
            self.lo[i] = min(self.lo[i], p[i])
            self.hi[i] = max(self.hi[i], p[i])

    @property
    def empty(self) -> bool:
        return self.lo[0] == math.inf

    def size(self) -> list[float] | None:
        return None if self.empty else [round(self.hi[i] - self.lo[i], 4) for i in range(3)]


# --- image sizes from file headers (PNG, JPEG, WebP) ---

def image_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


# --- glTF ---

def read_gltf(path: Path) -> tuple[dict, bytes | None]:
    """The glTF JSON and, for .glb, the binary chunk."""
    return parse_gltf(Path(path).read_bytes(), Path(path).name)


def parse_gltf(data: bytes, name: str = "model") -> tuple[dict, bytes | None]:
    """Like read_gltf, from bytes already in memory."""
    path = Path(name)
    if data[:4] == b"glTF":
        magic, version, length = struct.unpack("<III", data[:12])
        if magic != GLB_MAGIC or version != 2:
            raise ValueError(f"{path.name}: not a glTF 2.0 binary")
        offset, doc, binary = 12, None, None
        while offset + 8 <= min(length, len(data)):
            chunk_length, chunk_type = struct.unpack("<II", data[offset:offset + 8])
            body = data[offset + 8:offset + 8 + chunk_length]
            if chunk_type == CHUNK_JSON:
                doc = json.loads(body)
            elif chunk_type == CHUNK_BIN and binary is None:
                binary = body
            offset += 8 + chunk_length
        if doc is None:
            raise ValueError(f"{path.name}: GLB without a JSON chunk")
        return doc, binary
    return json.loads(data), None


def _image_bytes(doc: dict, image: dict, binary: bytes | None, folder: Path | None) -> bytes | None:
    if "bufferView" in image and binary is not None:
        view = doc["bufferViews"][image["bufferView"]]
        start = view.get("byteOffset", 0)
        return binary[start:start + min(view["byteLength"], 4096)]
    uri = image.get("uri")
    if uri and folder is not None and not uri.startswith("data:"):
        file = folder / uri
        if file.is_file():
            with file.open("rb") as handle:
                return handle.read(65536)
    return None


def measure_gltf(path: Path) -> dict:
    path = Path(path)
    doc, binary = read_gltf(path)
    return measure_doc(doc, binary, path.parent)


def measure_doc(doc: dict, binary: bytes | None = None, folder: Path | None = None) -> dict:
    """measure_gltf for a glTF JSON already in memory. Without the binary chunk or the folder the
    textures sit in, max_texture_px is None; everything else comes from the JSON alone."""
    accessors, meshes, nodes = doc.get("accessors", []), doc.get("meshes", []), doc.get("nodes", [])
    box, triangles, vertices = Box(), 0, 0

    scenes = doc.get("scenes") or [{"nodes": [i for i in range(len(nodes))]}]
    roots = scenes[doc.get("scene", 0)].get("nodes", []) if scenes else []
    stack = [(i, _identity()) for i in roots]
    seen_depth = 0
    while stack and seen_depth < 100_000:
        seen_depth += 1
        index, parent = stack.pop()
        node = nodes[index]
        world = _multiply(parent, _trs(node))
        if "mesh" in node:
            for prim in meshes[node["mesh"]].get("primitives", []):
                position = accessors[prim["attributes"]["POSITION"]] if "POSITION" in prim.get("attributes", {}) else None
                if position is None:
                    continue
                lo, hi = position.get("min"), position.get("max")
                if lo and hi:
                    for corner in ((x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])):
                        box.add(_apply(world, corner))
                count = accessors[prim["indices"]]["count"] if "indices" in prim else position["count"]
                mode = prim.get("mode", 4)
                vertices += position["count"]
                triangles += count // 3 if mode == 4 else max(count - 2, 0) if mode in (5, 6) else 0
        stack.extend((child, world) for child in node.get("children", []))

    sizes = [s for s in (image_size(b) for b in (_image_bytes(doc, img, binary, folder)
                                                  for img in doc.get("images", [])) if b) if s]
    animations = doc.get("animations", [])
    return {
        "size": box.size(),  # [x, y (up), z] in file units (metres for glTF)
        "triangles": triangles,
        "vertices": vertices,
        "rigged": bool(doc.get("skins")),
        "animated": bool(animations),
        # Blender names FBX takes "Armature|Armature|Run"; the last part is the clip's own name
        "animations": [(a.get("name") or f"animation_{i}").split("|")[-1] for i, a in enumerate(animations)],
        "materials": len(doc.get("materials", [])),
        "max_texture_px": max((max(s) for s in sizes), default=None),
        "extensions": sorted(doc.get("extensionsUsed", [])),
    }


# --- OBJ ---

def measure_obj(path: Path) -> dict:
    box, triangles, vertices = Box(), 0, 0
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                try:
                    box.add((float(parts[1]), float(parts[2]), float(parts[3])))
                    vertices += 1
                except (IndexError, ValueError):
                    continue
            elif line.startswith("f "):
                triangles += max(len(line.split()) - 3, 0)
    return {"size": box.size(), "triangles": triangles, "vertices": vertices, "rigged": False,
            "animated": False, "animations": [], "materials": None, "max_texture_px": None, "extensions": []}


def measure(path: Path) -> dict:
    suffix = Path(path).suffix.lower()
    if suffix in (".glb", ".gltf"):
        return measure_gltf(path)
    if suffix == ".obj":
        return measure_obj(path)
    raise ValueError(f"cannot measure {suffix} files")
