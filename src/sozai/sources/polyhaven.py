"""Poly Haven (https://polyhaven.com): CC0 models, textures and HDRIs.

Read from the public API (by the maintainer for the published data, or on the user's machine with
`sozai build --live`); asset files are always downloaded from Poly Haven by the user's machine.
The API asks for a unique User-Agent (sozai.http sends one) and for a visible credit to Poly Haven
wherever its assets are shown, which the MCP server passes on to the agent.
"""
from __future__ import annotations

import time
from collections.abc import Iterable

NAME = "polyhaven"
API = "https://api.polyhaven.com"
CREDIT = "Powered by Poly Haven (polyhaven.com), CC0"

TYPES = {"hdris": "hdri", "textures": "material", "models": "model"}
CREATION = {"hdri": "photographed", "material": "scanned", "model": "modeled"}


def _flat(values) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, list):
            out.extend(str(v) for v in value)
        elif isinstance(value, str):
            out.append(value)
    return out


def normalise(asset_id: str, data: dict, kind: str) -> dict:
    dims = [d / 1000 for d in (data.get("dimensions") or []) if isinstance(d, (int, float)) and d > 0]
    if len(dims) == 3:  # the API gives Blender's [x, y, z] with z up; the index stores [width, height, depth]
        dims = [dims[0], dims[2], dims[1]]
    tags = list(dict.fromkeys([*(data.get("tags") or []), *(data.get("categories") or [])]))
    attributes = {k: v for k, v in (data.get("attributes") or {}).items() if isinstance(v, (str, list, bool))}
    if data.get("evs_cap"):
        attributes["evs_cap"] = data["evs_cap"]
    max_res = data.get("max_resolution") or []
    published = data.get("date_published")
    return {
        "id": f"{NAME}:{asset_id}",
        "source": NAME,
        "source_id": asset_id,
        "title": data.get("name") or asset_id,
        "url": f"https://polyhaven.com/a/{asset_id}",
        "authors": list((data.get("authors") or {}).keys()),
        "licence": "CC0-1.0",
        "redistributable": True,
        "credit": CREDIT,
        "kind": kind,
        "style": "photoreal",
        "creation": CREATION[kind],
        "category": data.get("category") or "/".join(data.get("categories") or []),
        "tags": tags,
        "description": data.get("description") or "",
        "size_x": dims[0] if len(dims) > 0 else None,
        "size_y": dims[1] if len(dims) > 1 else None,
        "size_z": dims[2] if len(dims) > 2 else None,
        "size_source": "api" if dims else None,
        "polycount": data.get("polycount"),
        "max_texture_px": max(max_res) if max_res else None,
        "tileable": True if kind == "material" else (False if kind == "model" else None),
        "rigged": False if kind == "model" else None,
        "animated": False if kind == "model" else None,
        "thumbnail_url": data.get("thumbnail_url"),
        "acquire": "direct",
        "downloads": {"files_api": f"{API}/files/{asset_id}"},
        "attributes": attributes,
        "popularity": data.get("download_count"),
        "published": time.strftime("%Y-%m-%d", time.gmtime(published)) if published else None,
    }


def fetch(http) -> Iterable[dict]:
    for api_type, kind in TYPES.items():
        listing = http.get_json(f"{API}/assets?t={api_type}")
        for asset_id, data in listing.items():
            yield normalise(asset_id, data, kind)
