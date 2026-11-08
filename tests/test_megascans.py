"""Megascans listing rows: mapping, search, Fab previews, no fetching."""
import json

import pytest

from conftest import RECORDS
from sozai import fetch, index, view
from sozai.protocol import ToolError
from sozai.search import MAX_VARIANTS, Searcher
from sozai.sources import megascans


def row(uid, title, kind="model", size_m=None, scan_area_m=None, tags=(), **extra):
    return {"source": "megascans", "source_id": uid, "title": title, "kind": kind,
            "source_url": f"https://www.fab.com/listings/{uid}", "authors": ["Quixel Megascans"],
            "category_path": "nature-terrain/rock", "tags": list(tags), "size_m": size_m,
            "scan_area_m": scan_area_m, "pbr_maps": ["base_colour", "normal", "roughness"],
            "tileable": kind == "material", "rigged": False, "animated": False, "surface_type": "rock",
            "biomes": ["forest"], "animation_role": "set dressing", "read_on": "2026-11-07", **extra}


def test_record_mapping():
    rock = megascans.record(row("u1", "Mossy  Rock", size_m=[0.44, 0.74, 0.41], tags=["moss", "Rock"]))
    # the listing's axis order varies, so the dims stay unordered (largest first) and no height is claimed
    assert rock["size_x"] is rock["size_y"] is rock["size_z"] is None
    assert rock["attributes"]["dimensions_m"] == [0.74, 0.44, 0.41]
    assert rock["title"] == "Mossy Rock" and rock["tags"] == ["moss", "rock"]
    assert rock["licence"] == "LicenseRef-Fab-Standard" and rock["redistributable"] is False
    assert rock["acquire"] == "human" and rock["downloads"]["fab_listing"].endswith("/u1")
    assert rock["variant_group"] == "megascans:model:mossy rock" and "Fab Standard License" in rock["credit"]
    surface = megascans.record(row("u2", "Forest Floor", kind="material", scan_area_m=[2.0, 3.0]))
    assert [surface["size_x"], surface["size_y"], surface["size_z"]] == [2.0, 3.0, None]
    assert surface["maps"] == ["color", "normal", "roughness"]
    assert megascans.record(row("u3", "Kenney thing") | {"source": "kenney"}) is None
    assert megascans.record(row("u4", "Odd", kind="hdri")) is None


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    rows = [megascans.record(row(f"tag{i}", "Graffiti Tag", kind="decal", scan_area_m=[2.0, 1.0])) for i in range(30)]
    rows += [megascans.record(row("boulder", "Granite Boulder", size_m=[1.8, 3.1, 2.2], tags=["rock", "boulder"])),
             megascans.record(row("pebble", "Granite Pebble", size_m=[0.1, 0.05, 0.08], tags=["rock", "pebble"]))]
    path = tmp_path / "index.sqlite"
    index.build([dict(r) for r in RECORDS] + rows, path)
    return Searcher(index.connect(path))


def test_search_folds_same_titles_and_caps_variant_lists(mixed):
    found = mixed.search("graffiti", kind="decal")["results"]
    assert len(found) == 1 and found[0]["variants"] == 30 and found[0]["licence"] == "LicenseRef-Fab-Standard"
    asset = mixed.get(found[0]["id"])
    assert len(asset["variants"]) == MAX_VARIANTS and asset["variant_count"] == 29
    assert "fab.com/listings" in asset["how_to_get"] and "cannot download" in asset["how_to_get"]


def test_unordered_dims_feed_size_filters_but_not_height(mixed):
    big = mixed.search("granite", min_size_m=2)["results"]
    assert [r["id"] for r in big] == ["megascans:boulder"] and big[0]["dims_m"] == [3.1, 2.2, 1.8]
    tall = mixed.search("granite", min_height_m=2)
    assert tall["results"] == [] and tall["excluded_unknown_size"] == 2
    assert mixed.get("megascans:boulder")["dims_m"] == [3.1, 2.2, 1.8]


def test_redistributable_only_leaves_out_megascans(mixed):
    assert mixed.search("rock", redistributable_only=True)["results"][0]["id"] == "polyhaven:big_boulder"
    assert {r["source"] for r in mixed.search("rock")["results"]} == {"polyhaven", "megascans"}


def test_unknown_popularity_counts_as_typical(mixed):
    assert 0 < mixed._popularity(None) < mixed._popularity(5000)


def test_fetch_points_to_fab(mixed, tmp_path):
    with pytest.raises(ToolError, match="Fab"):
        fetch.fetch_asset(mixed.get("megascans:boulder"), str(tmp_path / "out"))


PAGE = """<html><head><meta property="og:image" content="https://media.fab.com/image_previews/gallery_images/P1/big.jpeg">
</head><body><script>{"images": [{"uid": "a", "width": 320, "height": 180, "size": 10376,
"url": "https://media.fab.com/image_previews/gallery_images/P1/small.jpg"}, {"uid": "b", "width": 640, "height": 360,
"size": 35467, "url": "https://media.fab.com/image_previews/gallery_images/P1/mid.jpg"}, {"uid": "c", "width": 1280,
"height": 720, "size": 137335, "url": "https://media.fab.com/image_previews/gallery_images/P1/large.jpg"},
{"uid": "d", "width": 640, "height": 360, "size": 1, "url": "https://media.fab.com/image_previews/gallery_images/OTHER/x.jpg"}]}
</script></body></html>"""


def test_fab_preview_picks_the_640_image_of_the_link_preview_and_remembers(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    listing = "https://www.fab.com/listings/u1"
    url = view.fab_preview_url(listing, page=PAGE)
    assert url == "https://media.fab.com/image_previews/gallery_images/P1/mid.jpg"
    assert json.loads((tmp_path / "cache" / "fab_previews.json").read_text()) == {listing: url}
    assert view.fab_preview_url(listing) == url  # remembered: the page is not read again
    assert view.fab_preview_url("https://example.test/listing") is None


def test_view_megascans_uses_fab_preview_and_survives_errors(tmp_path, monkeypatch):
    from test_measure import png

    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    good = {"id": "megascans:a", "title": "A", "source": "megascans", "url": "https://www.fab.com/listings/a"}
    bad = {"id": "megascans:b", "title": "B", "source": "megascans", "url": "https://www.fab.com/listings/b"}

    def preview(listing, page=None):
        if listing.endswith("/b"):
            raise OSError("HTTP Error 403")
        return "https://media.fab.com/x.jpg"

    monkeypatch.setattr(view, "fab_preview_url", preview)
    monkeypatch.setattr(view, "thumbnail_bytes", lambda url: png(4, 4))
    content = view.view([good, bad])
    assert content.structured == {"shown": ["megascans:a"]}
    assert "Fab preview unavailable" in content.blocks[-1]["text"] and "fab.com/listings/b" in content.blocks[-1]["text"]
