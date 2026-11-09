# Changelog

Release notes are written by hand for every tag. Data releases (`data-YYYY.MM.DD`) are listed
with the code release that first expects them.

## Unreleased

- New libraries: KayKit (Kay Lousberg, 903 models in 10 packs) and Polygonal Mind (991 models in
  18 packs, GLB versions by ToxSam), both CC0 and read from their GitHub repositories. `fetch_asset`
  downloads them straight from GitHub (pinned to the commit Sozai Search measured) and packs one
  `.glb`; KayKit packs are calibrated to metres where a pack has an object of known size.
- `sozai ingest kaykit` / `sozai ingest polygonalmind` (maintainers): measure every model from its
  glTF JSON without cloning (a Range request reads only a GLB's JSON chunk), render missing
  previews, and fall back to `git` for file lists when the GitHub API rate limit is reached.
- Models of one pack that differ only by a colour word fold into one result with variants.

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
