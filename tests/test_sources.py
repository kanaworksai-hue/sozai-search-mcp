import pytest

from sozai.sources import ambientcg, polyhaven


def test_polyhaven_model_sizes_are_metres():
    rec = polyhaven.normalise("ArmChair_01", {
        "name": "Arm Chair 01", "categories": ["furniture"], "tags": ["chair"], "polycount": 5626,
        "dimensions": [848.4, 765.7, 1065.0], "max_resolution": [4096, 4096], "download_count": 10,
        "authors": {"Kirill": "All"}, "date_published": 1585605600, "category": "Furniture/Seating/Chairs",
        "attributes": {"condition": ["worn"]}, "thumbnail_url": "https://cdn.test/t.webp"}, "model")
    # Blender z-up [x, y, z] becomes [width, height, depth]
    assert rec["size_x"] == pytest.approx(0.8484) and rec["size_y"] == pytest.approx(1.065)
    assert rec["size_z"] == pytest.approx(0.7657)
    assert rec["kind"] == "model" and rec["acquire"] == "direct" and rec["rigged"] is False
    assert rec["tags"] == ["chair", "furniture"] and rec["published"] == "2020-03-30"  # 22:00 UTC
    assert "Poly Haven" in rec["credit"]


def test_polyhaven_texture_and_hdri():
    tex = polyhaven.normalise("aerial_asphalt_01", {"name": "Aerial Asphalt 01", "dimensions": [30000, 30000]}, "material")
    assert tex["size_x"] == 30.0 and tex["size_z"] is None and tex["tileable"] is True
    hdri = polyhaven.normalise("sky", {"name": "Sky", "evs_cap": 14}, "hdri")
    assert hdri["size_x"] is None and hdri["attributes"]["evs_cap"] == 14


def test_ambientcg_material():
    rec = ambientcg.normalise({
        "id": "PavingStones152", "type": "material", "title": "Paving Stones 152", "technique": "surface-photogrammetry",
        "tags": ["152", "pavingstones152", "old", "paving"], "colors": ["A01010", "614736"],
        "dimensions": {"width": 130, "height": 130, "depth": 0}, "maps": ["color", "normal"],
        "downloads": [{"attributes": "2K-JPG", "url": "https://ambientcg.com/get?file=P_2K-JPG.zip", "size": 1},
                      {"attributes": "8K-PNG", "url": "https://ambientcg.com/get?file=P_8K-PNG.zip", "size": 2}],
        "downloadStatistics": {"total": 1216}, "relations": {"variations": []},
        "thumbnails": {"256-PNG": "https://acg.test/t.png"}, "releaseDate": "2026-11-05"})
    assert rec["kind"] == "material" and rec["creation"] == "scanned" and rec["style"] == "photoreal"
    assert rec["tags"] == ["old", "paving"]
    assert rec["size_x"] == 1.3 and rec["size_z"] is None
    assert rec["max_texture_px"] == 8192 and rec["tileable"] is True
    assert rec["attributes"]["colors"] == ["brown", "red"]
    assert rec["category"] == "Paving Stones"


def test_ambientcg_procedural_is_not_stylized_and_models_are_kept():
    rec = ambientcg.normalise({"id": "AcousticFoam001", "type": "material", "technique": "surface-procedural"})
    assert rec["creation"] == "procedural" and rec["style"] == "photoreal"
    model = ambientcg.normalise({"id": "3DTreeStump001", "type": "3d-model", "tags": ["3d", "stump"]})
    assert model["kind"] == "model" and model["tags"] == ["stump"] and model["tileable"] is False
    assert ambientcg.normalise({"id": "X", "type": "hdri-element"}) is None


def test_colour_names():
    assert ambientcg.colour_name("000000") == "black"
    assert ambientcg.colour_name("FFFFFF") == "white"
    assert ambientcg.colour_name("808080") == "grey"
    assert ambientcg.colour_name("2050E0") == "blue"
    assert ambientcg.colour_name("zz") is None
