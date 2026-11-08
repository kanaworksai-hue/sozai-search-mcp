"""Pack a .gltf (or a .glb that points at external files) into one self-contained .glb.

Standard library only. External buffers and images are appended to the binary chunk and their
URIs replaced by buffer views.
"""
from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from urllib.parse import unquote

from sozai.measure import read_gltf

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ktx2": "image/ktx2"}


def _read_uri(uri: str, folder: Path) -> tuple[bytes, str | None]:
    if uri.startswith("data:"):
        header, _, payload = uri.partition(",")
        mime = header[5:].split(";")[0] or None
        return (base64.b64decode(payload) if ";base64" in header else unquote(payload).encode()), mime
    path = (folder / unquote(uri)).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"{uri}: points outside the model folder")
    return path.read_bytes(), MIME.get(path.suffix.lower())


def needs_packing(path: Path) -> bool:
    """True for a .gltf, or a .glb whose buffers or images live in other files."""
    path = Path(path)
    if path.suffix.lower() == ".gltf":
        return True
    doc, _ = read_gltf(path)
    return any("uri" in item for item in doc.get("buffers", []) + doc.get("images", []))


def pack(source: Path, glb_path: Path) -> Path:
    source, glb_path = Path(source), Path(glb_path)
    folder = source.parent
    doc, glb_binary = read_gltf(source)
    blob = bytearray()

    def append(data: bytes) -> int:
        offset = len(blob)
        blob.extend(data)
        blob.extend(b"\0" * (-len(blob) % 4))
        return offset

    offsets = []
    for buffer in doc.get("buffers", []):
        if "uri" in buffer:
            data, _ = _read_uri(buffer["uri"], folder)
        else:  # the .glb's own binary chunk
            data = glb_binary or b""
        offsets.append(append(data[:buffer.get("byteLength", len(data))]))
    for view in doc.get("bufferViews", []):
        view["byteOffset"] = view.get("byteOffset", 0) + offsets[view["buffer"]]
        view["buffer"] = 0
    views = doc.setdefault("bufferViews", [])
    for image in doc.get("images", []):
        if "uri" not in image:
            continue
        data, mime = _read_uri(image.pop("uri"), folder)
        views.append({"buffer": 0, "byteOffset": append(data), "byteLength": len(data)})
        image["bufferView"] = len(views) - 1
        image["mimeType"] = image.get("mimeType") or mime or "image/png"
    if blob:
        doc["buffers"] = [{"byteLength": len(blob)}]
    else:
        doc.pop("buffers", None)
    return write_glb(doc, bytes(blob), glb_path)


def rescale(path: Path, factor: float) -> Path:
    """Scale a whole model in place by wrapping the scene's root nodes in one scaled node.

    Only the JSON changes; meshes, skins and animations keep working because the scale sits above
    every joint. Used to bring kits that are not modelled in metres to real size.
    """
    path = Path(path)
    doc, binary = read_gltf(path)
    nodes = doc.setdefault("nodes", [])
    scenes = doc.setdefault("scenes", [{"nodes": list(range(len(nodes)))}])
    scene = scenes[doc.get("scene", 0)]
    nodes.append({"name": "sozai_real_size", "scale": [factor, factor, factor], "children": scene.get("nodes", [])})
    scene["nodes"] = [len(nodes) - 1]
    if path.suffix.lower() == ".gltf":
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        return path
    return write_glb(doc, binary or b"", path)


def write_glb(doc: dict, blob: bytes, glb_path: Path) -> Path:
    body = json.dumps(doc, separators=(",", ":")).encode()
    body += b" " * (-len(body) % 4)
    chunks = struct.pack("<II", len(body), 0x4E4F534A) + body
    if blob:
        chunks += struct.pack("<II", len(blob), 0x004E4942) + bytes(blob)
    glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_path.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks)
    return glb_path
