"""A small offline index built from hand-written records (no network)."""
import pytest

from sozai import convert, index

REAL_FIND_BLENDER = convert.find_blender


@pytest.fixture(autouse=True)
def no_blender(monkeypatch):
    """Tests never start Blender unless they ask for it (see test_thumbs.py)."""
    monkeypatch.setattr(convert, "find_blender", lambda: None)


def record(source_id, title, kind="model", source="polyhaven", size=None, tags=(), description="",
           popularity=100, variant_group=None, tileable=None, polycount=None, licence="CC0-1.0"):
    size = list(size or [])
    return {
        "id": f"{source}:{source_id}", "source": source, "source_id": source_id, "title": title,
        "url": f"https://example.test/{source_id}", "authors": ["Someone"], "licence": licence,
        "credit": "credit line", "kind": kind, "style": "photoreal", "creation": "modeled",
        "category": "Things/Stuff", "tags": list(tags), "description": description,
        "size_x": size[0] if len(size) > 0 else None, "size_y": size[1] if len(size) > 1 else None,
        "size_z": size[2] if len(size) > 2 else None, "size_source": "api" if size else None,
        "polycount": polycount, "tileable": tileable, "thumbnail_url": f"https://example.test/{source_id}.png",
        "acquire": "direct" if source == "polyhaven" else "zip",
        "downloads": {"files_api": f"https://api.polyhaven.com/files/{source_id}"} if source == "polyhaven"
        else {"zips": [{"attributes": "2K-JPG", "url": f"https://ambientcg.com/get?file={source_id}_2K-JPG.zip", "size": 2_000_000}]},
        "attributes": {}, "variant_group": variant_group, "popularity": popularity, "published": "2026-01-01",
    }


RECORDS = [
    record("wooden_crate_01", "Wooden Crate 01", size=[0.8, 0.4, 0.35], tags=["crate", "wood", "box"], polycount=6000),
    record("wooden_crate_02", "Wooden Crate 02", size=[1.2, 0.5, 0.46], tags=["crate", "wood"], polycount=5000, popularity=50),
    record("metal_crate", "Metal Crate", size=[1.0, 1.0, 1.0], tags=["crate", "metal"], polycount=900),
    record("wooden_chair", "Wooden Chair", size=[0.5, 0.5, 0.9], tags=["chair", "wood", "furniture"], polycount=3000, popularity=900),
    record("old_barrel", "Old Barrel", tags=["barrel", "wood"]),  # unknown size
    record("big_boulder", "Big Boulder", size=[2.5, 2.0, 1.8], tags=["rock", "boulder"], polycount=120000),
    record("sofa_01", "Sofa 01", size=[1.6, 0.7, 0.8], tags=["sofa", "furniture"]),
    record("Bricks001", "Bricks 001", kind="material", source="ambientcg", size=[1.0, 1.0], tags=["bricks", "red", "wall"],
           tileable=True, variant_group="ambientcg:Bricks001"),
    record("Bricks002", "Bricks 002", kind="material", source="ambientcg", size=[1.0, 1.0], tags=["bricks", "red", "wall"],
           tileable=True, variant_group="ambientcg:Bricks001", popularity=10),
    record("sunset_sky", "Sunset Sky", kind="hdri", tags=["sunset", "sky", "outdoor"], popularity=5000),
]


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    path = tmp_path / "index.sqlite"
    index.build([dict(r) for r in RECORDS], path, sources={"polyhaven": 8, "ambientcg": 2})
    return path


@pytest.fixture
def searcher(index_path):
    from sozai.search import Searcher

    return Searcher(index.connect(index_path))
