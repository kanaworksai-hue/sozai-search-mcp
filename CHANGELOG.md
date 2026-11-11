# Changelog

Release notes are written by hand for every tag. Data releases (`data-YYYY.MM.DD`) are listed
with the code release that first expects them.

## Unreleased

- When semantic search is not set up, the first search of a session says so in its note: what
  it would give, its size (about 600 MB once) and how to turn it on, for the agent to pass on
  to the user. `SOZAI_SEMANTIC=0` turns the note off. Before, search fell back to keywords
  silently, so agents never mentioned it.
- The agent instructions say to pass that note on once and ask before setting semantic search up.
- READMEs (six languages): the install steps tell the agent to ask the user before turning on
  semantic search, with what it gives and its disk size; the data download is about 15 MB.

## 0.2.1 (2026-11-12)

Fixes found by reviewing 0.2.0. Expects published data
[`data-2026.11.12`](https://github.com/kanaworksai-hue/sozai-search-mcp/releases/tag/data-2026.11.12)
(the same assets as 2026.11.09, with fixed variant groups); the first search after updating
downloads it. No licence or breaking changes.

- `redistributable_only=true` keeps CC0 assets only, as documented. In 0.2.0 it also let NASA
  models through, which come with NASA's own terms.
- `view_assets` shows avatar previews over 2 MB when Pillow is installed (it downloads up to
  32 MB to shrink them; 0.2.0 refused them as "larger than 1 MB"). Without Pillow it no longer
  downloads previews it cannot show and gives their link straight away.
- Variants fold only models of about the same size, so a 42 m tower and its 0.4 m lamp, or a
  room and the small lights in it, are separate results again. NASA folds only the lettered
  versions of one object (Aqua (A), (B), (C)); numbers mark different objects (ICESat and
  ICESat-2, Landsat 7 and 8).
- Maintainers: `sozai ingest` gives a numbered id to a model whose id is already taken, instead
  of producing a catalog the index build would reject; KayKit's same-name check now works.

## 0.2.0 (2026-11-11)

Expects published data [`data-2026.11.09`](https://github.com/kanaworksai-hue/sozai-search-mcp/releases/tag/data-2026.11.09)
(38,943 records, up from 36,287); the first search after updating downloads it. No licence or
breaking changes.

- New libraries: KayKit (Kay Lousberg, 903 models in 10 packs) and Polygonal Mind (991 models in
  18 packs, GLB versions by ToxSam), both CC0 and read from their GitHub repositories. `fetch_asset`
  downloads them straight from GitHub (pinned to the commit Sozai Search measured) and packs one
  `.glb`; KayKit packs are calibrated to metres where a pack has an object of known size.
- `sozai ingest kaykit` / `sozai ingest polygonalmind` (maintainers): measure every model from its
  glTF JSON without cloning (a Range request reads only a GLB's JSON chunk), render missing
  previews, and fall back to `git` for file lists when the GitHub API rate limit is reached.
- Models of one pack that differ only by a colour word fold into one result with variants.
- New library: Open Source Avatars, 512 rigged CC0 VRM avatars (Polygonal Mind's 100Avatars R1-R3,
  their voxel versions, and the NeonGlitch86 collection), read from the registry on GitHub and
  measured from the files on Arweave. `fetch_asset` delivers one `.vrm`; FBX versions are listed
  where the registry has them. Avatars kept only on public IPFS gateways and the CC-BY collection
  are left out. `sozai ingest opensourceavatars` (maintainers).
- New library: NASA 3D Resources, 250 models of spacecraft, rovers, telescopes, rockets,
  asteroids and astronaut gear from NASA's GitHub repository, with descriptions and small previews
  from science.nasa.gov (read once per model page at ingest time, a second apart). They are under
  NASA's media usage guidelines rather than CC0: credit NASA, don't imply endorsement, ask before
  using NASA logos commercially; results show the licence. NASA files share no unit, so these
  models report no size in metres (the file's own units are in `attributes.size_units`).
  Folders with only `.blend`, `.3ds` or split `.7z` files are left out. `sozai ingest nasa`.
- Results show the licence of every asset that is not CC0 (before: only of assets that may not be
  redistributed).
- `view_assets` shrinks previews over 400 KB when Pillow is installed, and links them otherwise.
- Downloads cut off before their stated length are retried (up to three times) instead of being
  cached as broken files.
- The READMEs open with "Install with your AI agent": steps an agent can follow from the
  repository URL alone (uv, `uvx` registration for Claude Code, Codex and JSON clients, first
  search, optional semantic search).
- Maintainers: `sozai embed` reads preview images up to 64 MB, and Blender previews accept `.vrm`.

## 0.1.0 (2026-11-08)

First public release of Sozai Search MCP (package `sozai-search-mcp`, commands `sozai` and `sozai-search-mcp`,
Python module `sozai`).

- License: MIT, © 東京AI映像合同会社. The KanaWorks_AI brand assets (`docs/brand`, and Kana in
  `docs/assets`) are not MIT. Each indexed asset keeps its own licence; no asset files are distributed.

- MCP server over stdio, standard-library Python with no runtime dependencies: `search_assets`,
  `get_asset`, `view_assets`, `fetch_asset`, `update_index`.
- Index of about 36,000 free assets with real-world sizes in metres: Poly Haven and ambientCG
  (CC0), Kenney and Quaternius kits (CC0, measured and calibrated to metres), and Quixel
  Megascans listings (Fab links only).
- Published data release `data-2026.11.08`: records and search vectors in one zip, with its own
  `NOTICE.txt`; no asset files and no previews. Sozai Search moves to newer data automatically after an
  update, and falls back to the libraries' APIs when the release cannot be reached.
- Optional semantic search in any language (EmbeddingGemma 2, ONNX int8 query model pinned to one
  revision and checked by SHA-256), fused with keyword search; text and preview-image vectors.
- `fetch_asset` delivers one `.glb` per model (external textures packed, kit models rescaled to
  metres), PBR material folders and HDRIs, and keeps `CREDITS.md` up to date.
- `sozai ingest` for Kenney and Quaternius kits: measures every model, converts FBX with Blender,
  renders missing previews, reads each kit's licence file.
- Safety: downloads only over https from the libraries' own hosts (redirects included), every path
  built from data is kept inside the destination folder, fetches never delete existing files,
  archive size limits, local previews read only from Sozai Search's own catalog, and the semantic worker
  runs isolated from the user's project folder. A cross-process lock keeps two `sozai build` runs
  (or a build next to a running session) from breaking each other's data.
- Tested on macOS, Windows and Linux with Python 3.10 to 3.14 (CI on Ubuntu, macOS and Windows).
- README in English, 日本語, 简体中文, 한국어, Português and Español.
