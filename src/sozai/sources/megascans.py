"""Quixel Megascans listings (free on Fab), as records in the published data.

Megascans live on Fab, which has no public API; Sozai Search never crawls it. Their records reach users
through the published data release; the maintainer builds them from a listing table whose
location is given by the SOZAI_MEGASCANS_URL environment variable (JSON lines). Without it,
`sozai build --live` leaves Megascans out. sozai holds no Megascans files and fetches none: every
result points at its Fab listing, where the asset is free to claim. Previews come from that
listing's own link-preview image, one page at a time and only when an agent asks to look (see
view.py).

Sizes: a model's listing gives three dimensions in metres but not in a fixed axis order (a chair
reads [0.44, 0.74, 0.41], another [0.39, 0.47, 0.81]), so they are kept as unordered dims_m: the
largest drives the size filters, height filters skip them. Surfaces, decals and brushes have a
scanned area [width, height] in metres, which becomes the tile size.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable

from sozai.http import Http, open_url

NAME = "megascans"
URL_ENV = "SOZAI_MEGASCANS_URL"
CREDIT = "Quixel Megascans, free on Fab under the Fab Standard License"
MAPS = {"base_colour": "color", "displacement": "displacement", "translucency": "translucency"}
KINDS = {"model", "material", "decal", "brush"}


def _rows(url: str) -> Iterable[dict]:
    with open_url(url, timeout=120) as response:
        for line in response:
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def record(row: dict) -> dict | None:
    if row.get("source") != "megascans" or row.get("kind") not in KINDS or not row.get("source_id"):
        return None
    kind = row["kind"]
    size: list[float | None] = [None, None, None]
    dims = None
    size_m, area = row.get("size_m"), row.get("scan_area_m")
    if kind == "model" and size_m:
        dims = sorted((d for d in size_m if d), reverse=True) or None
    elif area and len(area) == 2:
        size = [area[0], area[1], None]  # a surface's scanned area: the real-world tile size
    title = re.sub(r"\s+", " ", row.get("title") or "").strip() or str(row["source_id"])
    category = row.get("category_path") or ""
    attributes = {k: v for k, v in {
        "surface": row.get("surface_type"),
        "biomes": row.get("biomes") or None,
        "role": row.get("animation_role"),
        "atlas": row.get("atlas") or None,
        "texel_density_px_m": row.get("texel_density_px_m"),
        "no_ai": row.get("no_ai") or None,
        "ai_generated": row.get("ai_generated") or None,
        "dimensions_m": dims,
        "listing_read_on": row.get("read_on"),
    }.items() if v not in (None, [], "")}
    return {
        "id": f"megascans:{row['source_id']}",
        "source": NAME,
        "source_id": row["source_id"],
        "title": title,
        "url": row.get("source_url"),
        "authors": row.get("authors") or ["Quixel Megascans"],
        "licence": "LicenseRef-Fab-Standard",
        "redistributable": False,
        "credit": CREDIT,
        "kind": kind,
        "style": "photoreal",
        "creation": "scanned",
        "category": category.replace("-", " ").replace("/", " / "),
        "tags": list(dict.fromkeys(t.lower() for t in row.get("tags") or [] if t)),
        "description": "",
        "size_x": size[0], "size_y": size[1], "size_z": size[2],
        "size_source": "listing (Fab)" if any(size) or dims else None,
        "polycount": row.get("triangles"),
        "max_texture_px": row.get("max_texture_px"),
        "maps": [MAPS.get(m, m) for m in row.get("pbr_maps") or []],
        "tileable": row.get("tileable"),
        "rigged": row.get("rigged"),
        "animated": row.get("animated"),
        "thumbnail_url": None,  # read from the Fab listing on demand (view.py)
        "acquire": "human",
        "downloads": {"fab_listing": row.get("source_url")},
        "attributes": attributes,
        # Megascans reuse titles a lot ("Rock Rough" x107): fold same-titled assets of one kind
        "variant_group": f"megascans:{kind}:{title.lower()}",
        "popularity": None,
        "published": None,
    }


def fetch(http: Http) -> Iterable[dict]:
    url = os.environ.get(URL_ENV)
    if not url:  # users get Megascans from the published data
        return
    for row in _rows(url):
        try:
            out = record(row)
        except (TypeError, ValueError, AttributeError):  # one malformed row must not end the build
            continue
        if out is not None:
            yield out
