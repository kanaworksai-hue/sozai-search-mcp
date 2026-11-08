"""Search quality check against the real local index.

A query counts as a hit at k when one of the top k results has an expected word in its id, title or tags.
Usage: uv run python eval/run.py [--k 5] [--verbose] [--semantic] [--queries eval/queries_semantic.json]
"""
import argparse
import json
import re
from pathlib import Path

from sozai import index, paths
from sozai.search import Searcher


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--semantic", action="store_true", help="fuse in semantic search (needs sozai-search-mcp[semantic])")
    parser.add_argument("--queries", default=str(Path(__file__).parent / "queries.json"))
    args = parser.parse_args()
    cases = json.loads(Path(args.queries).read_text())
    client = None
    if args.semantic:
        from sozai import semantic

        client = semantic.client()
    searcher = Searcher(index.connect(paths.index_path()))
    hits_at = {1: 0, args.k: 0}
    precision_sum = 0.0
    for case in cases:
        hits = client.search([case["query"], *case.get("alternatives", [])]) if client else None
        result = searcher.search(case["query"], case.get("alternatives"), limit=args.k, semantic=hits,
                                 **case.get("filters", {}))
        pattern = re.compile("|".join(case["expect"]), re.I)
        good = [bool(pattern.search(" ".join([r["id"], r["title"], *searcher.get(r["id"])["tags"]])))
                for r in result["results"]]
        hits_at[1] += bool(good[:1] and good[0])
        hits_at[args.k] += any(good)
        precision_sum += sum(good) / args.k
        if args.verbose or not any(good):
            mark = "ok  " if any(good) else "MISS"
            titles = ", ".join(r["title"] for r in result["results"])
            print(f"{mark} {case['query']!r} -> {titles}")
    n = len(cases)
    print(f"\n{n} queries | hit@1 {hits_at[1] / n:.0%} | hit@{args.k} {hits_at[args.k] / n:.0%} "
          f"| precision@{args.k} {precision_sum / n:.0%}")


if __name__ == "__main__":
    main()
