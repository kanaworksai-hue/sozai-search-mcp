"""The sozai MCP server (stdio) and command line.

Kept light on purpose: an MCP client starts one server per session, so the server uses only the
standard library, opens the SQLite index read-only per call and holds no state between calls.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from sozai import __version__, paths
from sozai.protocol import StdioServer, Tool, ToolError

INSTRUCTIONS = """\
Sozai Search finds free 3D assets with real-world sizes in metres: photoreal models, PBR materials and
HDRIs from Poly Haven and ambientCG (CC0); low-poly game models, many rigged and animated, from
Kenney, Quaternius, KayKit and Polygonal Mind (CC0); rigged VRM avatars from Open Source Avatars
(CC0); NASA spacecraft, rovers and telescopes (NASA media usage guidelines); and Quixel Megascans
scans (free on Fab, Fab Standard License).

How to search well:
- Write `query` as 1-4 English keywords for the thing itself ("wooden crate", "red brick", "sunset sky").
  Translate requests in other languages first. Put synonyms or other wordings in `alternatives`
  (["wooden box", "wood chest"]) instead of piling them into the query.
- Use the filters rather than words for kind, size and budget: kind="model" / "material" / "hdri",
  min_size_m / max_size_m on the largest dimension, min_height_m / max_height_m on the height,
  max_polycount, tileable, rigged / animated for characters. style="lowpoly" for game-ready
  low-poly models. redistributable_only=true keeps CC0 assets you may ship in an open project.
- Results are short on purpose. When several candidates fit, call view_assets to look at their
  thumbnails and pick. Call get_asset for the full record, and fetch_asset to put the files into
  the project (one .glb per model; quality web/game/film picks 1k/2k/4k textures).
- A result with "variants" stands for several assets of the same title (common in Megascans:
  many different rocks are all "Rock Rough"); get_asset lists them, view_assets shows them.
- size_m is [width, height, depth] in metres, height being the up axis; materials give
  [width, height] of the area one texture tile covers. dims_m (Megascans models) are the three
  dimensions in metres, largest first, without knowing which one is the height.
- NASA models have no size in metres (their files share no unit): scale them against the real
  object. Credit NASA and do not imply NASA endorsement.
- When a result's note says semantic search is off, pass that on to the user once and ask before
  setting it up; keep searching with English keywords meanwhile.
- Megascans cannot be fetched by Sozai Search: give the user the Fab link (free to claim with an Epic
  account, then download). Their files may be used in projects but not redistributed.

Credit: when you show or use Poly Haven assets, say "Powered by Poly Haven" (their API terms).
"""


KINDS = ["model", "material", "hdri", "decal", "atlas", "texture", "substance", "terrain", "brush"]
STYLES = ["photoreal", "stylized", "lowpoly"]
SOURCES = ["polyhaven", "ambientcg", "kenney", "quaternius", "kaykit", "polygonalmind", "opensourceavatars",
           "nasa", "megascans"]


def _one_or_many(values: list[str], description: str) -> dict:
    return {"anyOf": [{"type": "string", "enum": values},
                      {"type": "array", "items": {"type": "string", "enum": values}}],
            "description": description}


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "1-4 English keywords for the asset, e.g. 'wooden crate'. Empty = most popular assets that pass the filters."},
        "alternatives": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                         "description": "Other English wordings or synonyms, each searched separately, e.g. ['wooden box']."},
        "kind": _one_or_many(KINDS, "Asset kind, or a list of kinds."),
        "style": _one_or_many(STYLES, "Visual style, or a list of styles."),
        "source": _one_or_many(SOURCES, "Library, or a list of libraries."),
        "min_size_m": {"type": "number", "minimum": 0, "description": "Smallest allowed largest-dimension in metres. Assets of unknown size are left out."},
        "max_size_m": {"type": "number", "exclusiveMinimum": 0, "description": "Largest allowed largest-dimension in metres. Assets of unknown size are left out."},
        "min_height_m": {"type": "number", "minimum": 0, "description": "Smallest allowed height (up axis) in metres."},
        "max_height_m": {"type": "number", "exclusiveMinimum": 0, "description": "Largest allowed height (up axis) in metres, e.g. 'a crate under 1 m tall'."},
        "tileable": {"type": "boolean", "description": "true = only seamless, repeating surfaces."},
        "rigged": {"type": "boolean", "description": "true = only models with a skeleton (characters, animals)."},
        "animated": {"type": "boolean", "description": "true = only models that ship animation clips (get_asset lists them)."},
        "max_polycount": {"type": "integer", "exclusiveMinimum": 0, "description": "Polygon budget for models."},
        "redistributable_only": {"type": "boolean", "description": "true = only CC0 assets whose files may be shipped "
                                 "(e.g. in an open-source game); leaves out Megascans and NASA models (their own terms)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Results per page (default 10)."},
        "offset": {"type": "integer", "minimum": 0, "description": "Skip this many results (paging)."},
    },
}
GET_SCHEMA = {
    "type": "object",
    "properties": {"asset_id": {"type": "string", "description": "An id from search_assets, e.g. 'polyhaven:wooden_crate_01'."}},
    "required": ["asset_id"],
}
UPDATE_SCHEMA = {"type": "object", "properties": {}}
VIEW_SCHEMA = {
    "type": "object",
    "properties": {"asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                                 "description": "Ids from search_assets, best candidates first."}},
    "required": ["asset_ids"],
}
FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_id": {"type": "string", "description": "An id from search_assets."},
        "dest": {"type": "string", "description": "Absolute path of the folder to put the files in (e.g. the project's assets folder). Created if missing; existing files other than the asset's own are left alone."},
        "quality": {"type": "string", "enum": ["web", "game", "film"],
                    "description": "Picks the texture resolution: web 1k, game 2k (default), film 4k; film HDRIs are .exr."},
        "resolution": {"type": "string", "enum": ["1k", "2k", "4k", "8k"], "description": "Overrides quality's resolution."},
        "format": {"type": "string", "enum": ["glb", "gltf"],
                   "description": "Models: glb = one self-contained file (default); gltf = separate .gltf, .bin and textures."},
    },
    "required": ["asset_id", "dest"],
}


@contextmanager
def _searcher():
    from sozai import index
    from sozai.search import Searcher

    conn = index.connect(paths.index_path())
    try:
        yield Searcher(conn)
    finally:
        conn.close()  # an open handle stops `sozai build` from replacing the index on Windows


_checked_data = False


def _ensure_index() -> str | None:
    """Build the index on first use, and move to newer published data once per session after an
    upgrade of sozai. Returns a note for the agent when it did either."""
    global _checked_data
    from sozai.build import build_index

    if not paths.index_path().exists():
        _checked_data = True
        built = build_index()
        return f"Built the local index on first use: {built['count']} assets from {', '.join(built['sources'])}."
    if _checked_data:
        return None
    _checked_data = True
    from sozai import bundle

    installed = bundle.installed()
    if installed is None or not bundle.outdated(installed):  # up to date, or built live / by hand
        return None
    quiet = lambda message: print(message, file=sys.stderr)  # noqa: E731 (stdout carries the protocol)
    with paths.data_lock():  # the data we install is the data we index
        try:
            bundle.install(log=quiet)
        except Exception:  # offline or not released: keep searching the data we have
            return None
        built = build_index(log=quiet, refresh=False)
    return f"Updated the local index to published data {built['data']}: {built['count']} assets."


def search_assets(query: str = "", alternatives: list[str] | None = None, kind=None, style=None, source=None,
                  min_size_m: float | None = None, max_size_m: float | None = None,
                  min_height_m: float | None = None, max_height_m: float | None = None,
                  tileable: bool | None = None, rigged: bool | None = None, animated: bool | None = None,
                  max_polycount: int | None = None, redistributable_only: bool | None = None,
                  limit: int = 10, offset: int = 0) -> dict:
    note = _ensure_index()
    semantic_hits, semantic_note = _semantic(query, alternatives)
    with _searcher() as searcher:
        result = searcher.search(query, alternatives, limit=limit, offset=offset, semantic=semantic_hits,
                                 kind=kind, style=style,
                                 source=source, min_size_m=min_size_m, max_size_m=max_size_m,
                                 min_height_m=min_height_m, max_height_m=max_height_m, tileable=tileable,
                                 rigged=rigged, animated=animated, max_polycount=max_polycount,
                                 redistributable_only=bool(redistributable_only))
    notes = [n for n in (note, semantic_note, result.get("note")) if n]
    if notes:
        result["note"] = " ".join(notes)
    return result


SEMANTIC_FROM = '"sozai-search-mcp[semantic] @ git+https://github.com/kanaworksai-hue/sozai-search-mcp"'
_semantic_hint_given = False


def semantic_hint() -> str:
    from sozai import semantic

    missing = ("its Python packages are not installed" if not semantic.deps_installed()
               else "the query model is not downloaded" if not semantic.model_installed()
               else "the asset vectors are missing; run `sozai build`")
    return (f"Semantic search is off ({missing}), so this search used keywords only. Turned on, it makes "
            "descriptive and non-English searches much better; it needs about 600 MB once (Python packages and "
            "the query model). Tell the user once and set it up only if they agree: with uvx, run "
            f"`uvx --from {SEMANTIC_FROM} sozai semantic-setup` and register the server with that --from; "
            "from a clone, `uv sync --extra semantic`, then `uv run sozai semantic-setup`. "
            "SOZAI_SEMANTIC=0 in the server's environment turns this note off.")


def _semantic(query: str, alternatives: list[str] | None) -> tuple[dict | None, str | None]:
    """Ranked ids by meaning from the semantic worker, or (None, why not)."""
    global _semantic_hint_given
    texts = [t for t in [query, *(alternatives or [])] if t and t.strip()]
    if not texts or os.environ.get("SOZAI_SEMANTIC") == "0":
        return None, None
    from sozai import semantic

    missing = semantic.status()
    if missing:  # keyword search still works; say once per session what the user is missing
        if _semantic_hint_given:
            return None, None
        _semantic_hint_given = True
        return None, semantic_hint()
    try:
        return semantic.client().search(texts), None
    except Exception as error:
        return None, f"Semantic search failed ({error}); these are keyword results."


def get_asset(asset_id: str) -> dict:
    _ensure_index()
    with _searcher() as searcher:
        asset = searcher.get(asset_id)
    if asset is None:
        raise ToolError(f"No asset {asset_id!r}. Use an id returned by search_assets.")
    return asset


def update_index() -> dict:
    from sozai.build import build_index

    return build_index()


def view_assets(asset_ids: list[str]):
    from sozai.view import view

    assets = []
    for asset_id in asset_ids:  # an unknown id should not hide the others
        try:
            assets.append(get_asset(asset_id))
        except ToolError as error:
            assets.append({"id": asset_id, "error": str(error)})
    return view(assets)


def fetch_asset(asset_id: str, dest: str, quality: str = "game", resolution: str | None = None,
                format: str = "glb") -> dict:
    from sozai.fetch import fetch_asset as fetch

    return fetch(get_asset(asset_id), dest, quality=quality, resolution=resolution, fmt=format)


TOOLS = [
    Tool("search_assets",
         "Search free 3D models, PBR materials, decals and HDRIs (CC0 libraries and Megascans). Returns short results "
         "(id, title, kind, size in metres, polycount, thumbnail).",
         SEARCH_SCHEMA, search_assets, {"readOnlyHint": True, "openWorldHint": False}),
    Tool("get_asset",
         "Full record of one asset: sizes, maps, tags, licence and credit, and how to download its files.",
         GET_SCHEMA, get_asset, {"readOnlyHint": True, "openWorldHint": False}),
    Tool("view_assets",
         "Look at the thumbnails of up to 8 assets (ids from search_assets), numbered, to choose between them.",
         VIEW_SCHEMA, view_assets, {"readOnlyHint": True, "openWorldHint": True}),
    Tool("fetch_asset",
         "Download an asset into a folder, ready to use: models as one .glb; materials as a folder of PBR maps "
         "(Poly Haven adds material.json with the tile size, ambientCG ready-made .tres/.mtlx/.blend materials); "
         "HDRIs as .hdr/.exr. Adds the credit line to CREDITS.md there. Returns the file paths.",
         FETCH_SCHEMA, fetch_asset, {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}),
    Tool("update_index",
         "Rebuild the local index from the published data (downloading it when this sozai version expects newer "
         "data) plus kits ingested on this machine. Use when assets seem missing or stale.",
         UPDATE_SCHEMA, update_index, {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True}),
]


def serve() -> None:
    StdioServer("sozai", __version__, INSTRUCTIONS, TOOLS).run()


def _print(data) -> None:
    if not sys.stdout.isatty() and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to the ANSI code page (cp932, cp1252)
    print(json.dumps(data, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> None:
    prog = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    parser = argparse.ArgumentParser(prog=prog if prog in ("sozai", "sozai-search-mcp") else "sozai",
                                     description="Sozai Search MCP: free 3D assets for AI agents.")
    parser.add_argument("--version", action="version", version=f"sozai-search-mcp {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("mcp", help="run the MCP server on stdin/stdout (the default)")
    build = commands.add_parser("build", help="build the local index from the published data (downloaded once)")
    build.add_argument("--live", action="store_true", help="read every library's API instead (newest assets)")
    build.add_argument("--data", help="install this data zip (path or https URL) first")
    pack = commands.add_parser("pack", help="maintainers: write the data zip for a release")
    pack.add_argument("--out", default="dist")
    pack.add_argument("--version")
    search = commands.add_parser("search", help="search from the command line")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--alt", action="append", help="an alternative wording (repeatable)")
    search.add_argument("--kind", action="append", choices=KINDS)
    search.add_argument("--source", action="append", choices=SOURCES)
    search.add_argument("--min-size", type=float)
    search.add_argument("--max-size", type=float)
    search.add_argument("--max-polycount", type=int)
    search.add_argument("--tileable", action="store_true", default=None)
    search.add_argument("--limit", type=int, default=10)
    get = commands.add_parser("get", help="show one asset")
    get.add_argument("asset_id")
    commands.add_parser("where", help="print the index location")
    fetch = commands.add_parser("fetch", help="download an asset into a folder")
    fetch.add_argument("asset_id")
    fetch.add_argument("dest")
    fetch.add_argument("--quality", choices=["web", "game", "film"], default="game")
    fetch.add_argument("--resolution", choices=["1k", "2k", "4k", "8k"])
    fetch.add_argument("--format", choices=["glb", "gltf"], default="glb")
    ingest = commands.add_parser("ingest", help="catalog a folder of kit zips (kenney, quaternius) or a library "
                                 "on GitHub (maintainers: kaykit, polygonalmind, opensourceavatars, nasa), then rebuild")
    ingest.add_argument("source", choices=["kenney", "quaternius", "kaykit", "polygonalmind", "opensourceavatars", "nasa"])
    ingest.add_argument("folder", nargs="?", help="the folder of kit zips (kenney, quaternius only)")
    ingest.add_argument("--no-thumbnails", action="store_true",
                        help="skip rendering previews with Blender for models that ship without one")
    embed = commands.add_parser("embed", help="maintainers: compute asset vectors with MLX (needs sozai-search-mcp[embed])")
    embed.add_argument("--no-images", action="store_true", help="text vectors only")
    commands.add_parser("semantic-setup", help="download the query model for semantic search (needs sozai-search-mcp[semantic])")
    args = parser.parse_args(argv)

    if args.command in (None, "mcp"):
        serve()
    elif args.command == "build":
        from sozai import bundle
        from sozai.build import build_index

        with paths.data_lock():  # the data we install is the data we index
            if args.data:
                bundle.install(args.data, log=lambda m: print(m, file=sys.stderr))
            built = build_index(live=args.live, refresh=not args.data)
        _print(built)
    elif args.command == "pack":
        from sozai import bundle

        _print(bundle.pack(args.out, args.version or bundle.DATA_VERSION, log=lambda m: print(m, file=sys.stderr)))
    elif args.command == "search":
        _print(search_assets(args.query, args.alt, kind=args.kind, source=args.source, min_size_m=args.min_size,
                             max_size_m=args.max_size, max_polycount=args.max_polycount, tileable=args.tileable,
                             limit=args.limit))
    elif args.command == "get":
        try:
            _print(get_asset(args.asset_id))
        except ToolError as error:
            sys.exit(str(error))
    elif args.command == "where":
        print(paths.index_path())
    elif args.command == "fetch":
        try:
            dest = str(Path(args.dest).expanduser().resolve())  # the command line means "relative to here"
            _print(fetch_asset(args.asset_id, dest, args.quality, args.resolution, args.format))
        except ToolError as error:
            sys.exit(str(error))
    elif args.command == "embed":
        from sozai.embed_build import build_vectors

        _print(build_vectors(log=lambda m: print(m, file=sys.stderr), images=not args.no_images))
    elif args.command == "semantic-setup":
        from sozai import semantic

        if not semantic.deps_installed():
            sys.exit("install the semantic extra first: uv sync --extra semantic (or pip install 'sozai-search-mcp[semantic]')")
        semantic.install_model(log=lambda m: print(m, file=sys.stderr))
        _print({"model": str(semantic.model_dir()), "status": semantic.status() or "ready"})
    elif args.command == "ingest":
        from sozai import repos
        from sozai.catalog import ingest as ingest_kits

        log = lambda m: print(m, file=sys.stderr)  # noqa: E731
        if args.source in repos.LIBRARIES:
            report = repos.ingest(args.source, log=log, thumbnails=not args.no_thumbnails)
        elif not args.folder:
            sys.exit(f"sozai ingest {args.source} needs the folder of kit zips")
        else:
            report = ingest_kits(args.source, args.folder, log=log, thumbnails=not args.no_thumbnails)
        report["index"] = update_index()
        _print(report)


if __name__ == "__main__":
    main()
