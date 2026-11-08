"""Semantic search plumbing: fusion, the on-demand worker client, document text. No model needed."""
import sys
import textwrap

import pytest

from sozai import semantic
from sozai.search import Searcher

FAKE_WORKER = textwrap.dedent("""
    import json, os, sys, queue, threading
    print(json.dumps({"ready": True}), flush=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in iter(sys.stdin.readline, "")] or lines.put(""), daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=float(os.environ.get("IDLE", "30")))
        except queue.Empty:
            sys.exit(0)
        if not line:
            sys.exit(0)
        texts = json.loads(line)["texts"]
        print(json.dumps({"text": [[f"pid{os.getpid()}", 0.5]], "image": [], "texts": texts}), flush=True)
""")


def test_client_starts_worker_on_demand_and_restarts_after_idle_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))  # the worker runs from the data folder
    script = tmp_path / "worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setenv("IDLE", "0.3")
    client = semantic.Client([sys.executable, str(script)])
    assert client.process is None  # nothing runs until the first query
    first = client.search(["a chair"])
    assert first["texts"] == ["a chair"]
    first_pid = client.process.pid
    client.process.wait(timeout=5)  # the worker leaves on its own after the idle time
    second = client.search(["a table", "a desk"])
    assert second["texts"] == ["a table", "a desk"] and client.process.pid != first_pid
    client.close()


def test_fusion_ranks_agreement_first_and_respects_filters(searcher):
    hits = {"text": [["polyhaven:sofa_01", 0.7], ["polyhaven:wooden_chair", 0.6], ["ambientcg:Bricks001", 0.5]],
            "image": [["polyhaven:wooden_chair", 0.4], ["polyhaven:sofa_01", 0.3]]}
    found = searcher.search("chair", semantic=hits, kind="model")["results"]
    # the chair is in all three rankings; the bricks fail the kind filter
    assert [r["id"] for r in found] == ["polyhaven:wooden_chair", "polyhaven:sofa_01"]
    assert found[0]["score"] == pytest.approx(1.0, abs=0.02)


def test_semantic_alone_answers_queries_without_english_words(searcher):
    plain = searcher.search("木の椅子")
    assert plain["results"] == [] and "Translate" in plain["note"]
    found = searcher.search("木の椅子", semantic={"text": [["polyhaven:wooden_chair", 0.6]], "image": []})
    assert [r["id"] for r in found["results"]] == ["polyhaven:wooden_chair"] and "note" not in found


def test_doc_text_reads_like_a_titled_document():
    text = semantic.doc_text({"title": "Mossy Rock", "kind": "model", "style": "photoreal", "category": "nature / rock",
                              "tags": ["moss", "rock"], "attributes": {"surface": "rock", "collection": None}})
    assert text == "title: Mossy Rock | text: model. photoreal. nature rock. rock. moss, rock"


def test_status_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path))
    monkeypatch.setattr(semantic, "deps_installed", lambda: True)
    assert "semantic-setup" in semantic.status()
    monkeypatch.setattr(semantic, "deps_installed", lambda: False)
    assert "sozai-search-mcp[semantic]" in semantic.status()


def test_server_stays_keyword_only_without_semantic(index_path):
    from sozai import server

    result = server.search_assets("wooden crate")
    assert result["results"][0]["id"].startswith("polyhaven:wooden_crate") and "Semantic" not in result.get("note", "")
