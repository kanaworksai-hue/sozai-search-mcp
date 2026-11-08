from sozai.search import terms_of


def ids(result):
    return [r["id"] for r in result["results"]]


def test_terms_drop_stop_words_and_join_low_poly():
    assert terms_of("I need a free 3D low poly Wooden crate") == ["lowpoly", "wooden", "crate"]


def test_all_words_rank_above_some_words(searcher):
    top = ids(searcher.search("wooden crate"))
    assert set(top[:2]) == {"polyhaven:wooden_crate_01", "polyhaven:wooden_crate_02"}
    # partial matches still come back, below the full ones
    assert "polyhaven:metal_crate" in top and "polyhaven:wooden_chair" in top
    assert top.index("polyhaven:metal_crate") > 1


def test_whole_words_only(searcher):
    # "car" must not match "carpet"-style prefixes; nothing here is a car
    assert searcher.search("car")["results"] == []


def test_kind_and_size_filters(searcher):
    result = searcher.search("crate", kind="model", max_size_m=1.0)
    assert set(ids(result)) == {"polyhaven:wooden_crate_01", "polyhaven:metal_crate"}
    rocks = searcher.search("rock", min_size_m=2, max_size_m=3)
    assert ids(rocks) == ["polyhaven:big_boulder"]


def test_height_filter_uses_the_up_axis(searcher):
    # wooden_crate_01 is 0.8 wide but 0.4 tall; wooden_crate_02 is 0.5 tall; metal_crate 1.0 tall
    low = searcher.search("crate", max_height_m=0.45)
    assert ids(low) == ["polyhaven:wooden_crate_01"]
    assert set(ids(searcher.search("crate", min_height_m=0.45))) == {"polyhaven:wooden_crate_02", "polyhaven:metal_crate"}


def test_size_filter_reports_unknown_sizes(searcher):
    result = searcher.search("barrel", max_size_m=2)
    assert result["results"] == []
    assert result["excluded_unknown_size"] == 1


def test_rigged_filter(searcher):
    assert searcher.search("crate", rigged=True)["results"] == []
    assert len(searcher.search("crate", rigged=False)["results"]) == 0  # fixture rows leave rigged unknown


def test_polycount_and_tileable(searcher):
    assert "polyhaven:big_boulder" not in ids(searcher.search("rock boulder", max_polycount=50000))
    assert ids(searcher.search("bricks", tileable=True)) == ["ambientcg:Bricks001"]


def test_alternatives_find_synonyms(searcher):
    assert ids(searcher.search("couch"))== []
    assert ids(searcher.search("couch", alternatives=["sofa"])) == ["polyhaven:sofa_01"]


def test_variants_are_folded(searcher):
    result = searcher.search("red bricks")
    assert ids(result) == ["ambientcg:Bricks001"]
    assert result["results"][0]["variants"] == 2


def test_non_english_query_says_so(searcher):
    result = searcher.search("木箱")
    assert result["results"] == [] and "English" in result["note"]


def test_empty_query_lists_popular_with_filters(searcher):
    assert ids(searcher.search("", kind="hdri")) == ["polyhaven:sunset_sky"]


def test_compact_results_skip_empty_fields(searcher):
    hdri = searcher.search("sunset")["results"][0]
    assert "size_m" not in hdri and "polycount" not in hdri and "style" not in hdri
    crate = searcher.search("wooden crate")["results"][0]
    assert crate["size_m"] == [0.8, 0.4, 0.35]


def test_get_asset_has_download_instructions(searcher):
    crate = searcher.get("polyhaven:wooden_crate_01")
    assert crate["size_m"] == [0.8, 0.4, 0.35]
    assert "api.polyhaven.com/files/wooden_crate_01" in crate["how_to_get"]
    bricks = searcher.get("ambientcg:Bricks001")
    assert "2K-JPG" in bricks["how_to_get"] and bricks["variants"] == ["ambientcg:Bricks002"]
    assert searcher.get("polyhaven:nope") is None
