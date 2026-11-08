# Changelog

Release notes are written by hand for every tag. Data releases (`data-YYYY.MM.DD`) are listed
with the code release that first expects them.

## 0.1.0 (unreleased draft)

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
  runs isolated from the user's project folder.
