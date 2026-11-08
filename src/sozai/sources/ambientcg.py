"""ambientCG (https://ambientcg.com): CC0 materials, HDRIs, decals, atlases and a few models.

Read through the public v3 API. ambientCG is run by one person, so pages are large (500) and
requests are spaced out.
"""
from __future__ import annotations

import colorsys
import re
from collections.abc import Iterable

NAME = "ambientcg"
API = "https://ambientcg.com/api/v3/assets"
INCLUDE = "type,releaseDate,shortDescription,longDescription,title,url,tags,colors,dimensions,downloadStatistics,maps,technique,downloads,relations,thumbnails"

KINDS = {
    "material": "material", "hdri": "hdri", "decal": "decal", "atlas": "atlas", "3d-model": "model",
    "substance": "substance", "terrain": "terrain", "brush": "brush", "plain-texture": "texture",
}  # hdri-element (partial HDRI layers) is left out


def _creation(technique: str | None) -> str | None:
    technique = technique or ""
    if "photogrammetry" in technique or "multi-angle" in technique or "multiangle" in technique:
        return "scanned"
    if "procedural" in technique:
        return "procedural"
    if "approximat" in technique:
        return "approximated"
    return None


def colour_name(hex_colour: str) -> str | None:
    """A plain colour word for a hex value, so 'red brick' can match a red brick material."""
    try:
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return None
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < 0.12:
        return "black"
    if l > 0.9 and s < 0.3:
        return "white"
    if s < 0.15:
        return "grey"
    hue = h * 360
    if (hue < 45 or hue >= 345) and l < 0.45 and s < 0.6:
        return "brown"
    for limit, name in ((15, "red"), (45, "orange"), (70, "yellow"), (165, "green"), (200, "cyan"),
                        (255, "blue"), (290, "purple"), (345, "pink"), (360, "red")):
        if hue < limit:
            return name
    return None


def _tags(asset: dict) -> list[str]:
    """Library tags without the id fragments ambientCG adds ("152", "pavingstones152", "3d")."""
    asset_id = asset["id"].lower()
    out = []
    for tag in asset.get("tags") or []:
        tag = tag.strip().lower()
        if not tag or tag.isdigit() or tag == asset_id or tag == "3d" or re.fullmatch(r"[a-z]+\d+", tag):
            continue
        out.append(tag)
    return out


def normalise(asset: dict) -> dict | None:
    kind = KINDS.get(asset.get("type") or "")
    if not kind:
        return None
    dims = asset.get("dimensions") or {}
    width, height, depth = ((dims.get(k) or 0) / 100 or None for k in ("width", "height", "depth"))
    downloads = [{"attributes": d.get("attributes"), "url": d.get("url"), "size": d.get("size")}
                 for d in asset.get("downloads") or [] if d.get("url")]
    colours = [c for c in asset.get("colors") or [] if c]
    names = sorted({n for n in map(colour_name, colours) if n})
    description = asset.get("longDescription") or asset.get("shortDescription") or ""
    relations = asset.get("relations") or {}
    variations = relations.get("variations") or []
    title = asset.get("title") or asset["id"]
    return {
        "id": f"{NAME}:{asset['id']}",
        "source": NAME,
        "source_id": asset["id"],
        "title": title,
        "url": asset.get("url") or f"https://ambientcg.com/a/{asset['id']}",
        "authors": ["ambientCG"],
        "licence": "CC0-1.0",
        "redistributable": True,
        "credit": f"Created using {title} from ambientCG.com, licensed under CC0 1.0",
        "kind": kind,
        "style": "photoreal",
        "creation": _creation(asset.get("technique")),
        "category": re.sub(r"\s*\d+$", "", title),
        "tags": _tags(asset),
        "description": description,
        "size_x": width,
        "size_y": height,
        "size_z": depth,
        "size_source": "api" if (width or height) else None,
        "maps": asset.get("maps") or [],
        "max_texture_px": _max_px(downloads),
        "tileable": True if kind in ("material", "texture") else (False if kind in ("decal", "atlas", "model") else None),
        "rigged": False if kind == "model" else None,
        "animated": False if kind == "model" else None,
        "thumbnail_url": (asset.get("thumbnails") or {}).get("256-PNG"),
        "acquire": "zip",
        "downloads": {"zips": downloads},
        "attributes": {"colors": names, "technique": asset.get("technique")},
        "variant_group": f"{NAME}:{min([asset['id'], *variations])}" if variations else None,
        "popularity": (asset.get("downloadStatistics") or {}).get("total"),
        "published": asset.get("releaseDate"),
    }


def _max_px(downloads: list[dict]) -> int | None:
    best = 0
    for download in downloads:
        match = re.search(r"(\d+)K", download.get("attributes") or "")
        if match:
            best = max(best, int(match.group(1)) * 1024)
    return best or None


def fetch(http) -> Iterable[dict]:
    url = f"{API}?limit=500&offset=0&sort=popular&include={INCLUDE}"
    while url:
        page = http.get_json(url)
        for asset in page.get("assets") or []:
            record = normalise(asset)
            if record:
                yield record
        url = page.get("nextPageHttp")
