"""Search the index: hard filters first, then keyword relevance (FTS5 BM25), then popularity.

Ranking, per phrasing of the request:
- coverage: the share of the request's words an asset matches (all words beat some words),
- relevance: BM25 over title > tags > category > description > extra,
- popularity: the library's download count, log-scaled, as a light tie-breaker.
Alternative phrasings (synonyms written by the agent) are searched too; an asset keeps its best score.
"""
from __future__ import annotations

import math
import re
import sqlite3

from sozai.index import FTS_WEIGHTS, decode

STOP_WORDS = {
    "a", "an", "the", "of", "for", "with", "and", "or", "in", "on", "to", "at", "by", "from", "some", "any",
    "me", "i", "my", "need", "want", "find", "show", "get", "asset", "assets", "free", "cc0", "3d",
}
MAX_TERMS = 8
CANDIDATES = 300
W_COVERAGE, W_RELEVANCE, W_POPULARITY = 0.55, 0.3, 0.15
ALTERNATIVE_PENALTY = 0.97

COMPACT_FIELDS = ("id", "title", "kind", "source", "style", "size_m", "polycount", "tileable",
                  "max_texture_px", "thumbnail_url")


def terms_of(text: str) -> list[str]:
    words = re.findall(r"[0-9a-z]+", (text or "").lower().replace("low poly", "lowpoly"))
    out = []
    for word in words:
        if word not in STOP_WORDS and word not in out:
            out.append(word)
    return out[:MAX_TERMS]


def _as_list(value) -> list | None:
    if value is None or value == "" or value == []:
        return None
    return [value] if isinstance(value, str) else list(value)


MAX_VARIANTS = 24
RRF_K = 60
FUSION_WEIGHTS = {"keyword": 1.0, "text": 1.0, "image": 0.7}
SIZE_FILTERS = ("min_size_m", "max_size_m", "min_height_m", "max_height_m")


def _filters(kind=None, style=None, source=None, min_size_m=None, max_size_m=None, min_height_m=None,
             max_height_m=None, tileable=None, rigged=None, animated=None, max_polycount=None,
             redistributable_only=False) -> tuple[str, list]:
    clauses, params = [], []
    for column, value in (("kind", kind), ("style", style), ("source", source)):
        values = _as_list(value)
        if values:
            clauses.append(f"a.{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
    if min_size_m is not None:
        clauses.append("a.max_dim_m >= ?")
        params.append(min_size_m)
    if max_size_m is not None:
        clauses.append("a.max_dim_m <= ?")
        params.append(max_size_m)
    if min_height_m is not None:
        clauses.append("a.size_y >= ?")
        params.append(min_height_m)
    if max_height_m is not None:
        clauses.append("a.size_y <= ?")
        params.append(max_height_m)
    for column, value in (("tileable", tileable), ("rigged", rigged), ("animated", animated)):
        if value is not None:
            clauses.append(f"a.{column} = ?")
            params.append(int(bool(value)))
    if max_polycount is not None:
        clauses.append("a.polycount IS NOT NULL AND a.polycount <= ?")
        params.append(max_polycount)
    if redistributable_only:
        clauses.append("a.redistributable = 1")
    return (" AND ".join(clauses) or "1"), params


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


class Searcher:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        top = conn.execute("SELECT MAX(popularity) FROM assets").fetchone()[0] or 1
        self._log_top = math.log1p(top)
        # Libraries without download counts (Megascans, kits) count as typical, not as unpopular
        known = conn.execute("SELECT COUNT(*) FROM assets WHERE popularity IS NOT NULL").fetchone()[0]
        middle = conn.execute("SELECT popularity FROM assets WHERE popularity IS NOT NULL ORDER BY popularity "
                              "LIMIT 1 OFFSET ?", (known // 2,)).fetchone() if known else None
        self._typical = self._popularity(middle[0]) if middle else 0.0

    def _popularity(self, value) -> float:
        if value is None:
            return getattr(self, "_typical", 0.0)
        return math.log1p(value) / self._log_top if self._log_top else 0.0

    def _matching(self, fts_query: str, where: str, params: list, limit: int) -> list[tuple[int, float]]:
        weights = ", ".join(str(w) for w in FTS_WEIGHTS)
        sql = (f"SELECT a.rowid, bm25(assets_fts, {weights}) AS rank FROM assets_fts "
               f"JOIN assets a ON a.rowid = assets_fts.rowid "
               f"WHERE assets_fts MATCH ? AND {where} ORDER BY rank LIMIT ?")
        return [(row[0], -row[1]) for row in self.conn.execute(sql, [fts_query, *params, limit])]

    def _rowids_with(self, term: str, rowids: list[int]) -> set[int]:
        marks = ", ".join("?" for _ in rowids)
        sql = f"SELECT rowid FROM assets_fts WHERE assets_fts MATCH ? AND rowid IN ({marks})"
        return {row[0] for row in self.conn.execute(sql, [_quote(term), *rowids])}

    def _score_phrasing(self, terms: list[str], where: str, params: list) -> dict[int, float]:
        found: dict[int, float] = {}
        for row, rel in self._matching(" AND ".join(map(_quote, terms)), where, params, CANDIDATES):
            found[row] = rel
        if len(terms) > 1 and len(found) < CANDIDATES:
            for row, rel in self._matching(" OR ".join(map(_quote, terms)), where, params, CANDIDATES):
                found.setdefault(row, rel)
        if not found:
            return {}
        rowids = list(found)
        hits = {row: 0 for row in rowids}
        for term in terms:
            for row in self._rowids_with(term, rowids):
                hits[row] += 1
        top_rel = max(found.values()) or 1.0
        popularity = dict(self.conn.execute(
            f"SELECT rowid, popularity FROM assets WHERE rowid IN ({', '.join('?' for _ in rowids)})", rowids).fetchall())
        return {row: W_COVERAGE * hits[row] / len(terms) + W_RELEVANCE * rel / top_rel
                + W_POPULARITY * self._popularity(popularity.get(row)) for row, rel in found.items()}

    def _semantic_ranks(self, semantic: dict, where: str, params: list) -> dict[str, list[int]]:
        """The worker's ranked ids per modality, kept to the assets that pass the filters, as rowids."""
        out = {}
        for kind, ranked in semantic.items():
            ids = [asset_id for asset_id, _ in ranked]
            allowed: dict[str, int] = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                sql = f"SELECT a.id, a.rowid FROM assets a WHERE a.id IN ({', '.join('?' for _ in chunk)}) AND {where}"
                allowed.update(self.conn.execute(sql, [*chunk, *params]).fetchall())
            out[kind] = [allowed[i] for i in ids if i in allowed]
        return out

    @staticmethod
    def _fuse(rankings: dict[str, list[int]]) -> dict[int, float]:
        """Reciprocal rank fusion, scaled so an asset first in every ranking scores 1."""
        top = sum(FUSION_WEIGHTS.get(kind, 0) / (RRF_K + 1) for kind, rows in rankings.items() if rows)
        fused: dict[int, float] = {}
        for kind, rows in rankings.items():
            weight = FUSION_WEIGHTS.get(kind, 0)
            for rank, row in enumerate(rows, 1):
                fused[row] = fused.get(row, 0.0) + weight / (RRF_K + rank)
        return {row: score / top for row, score in fused.items()} if top else {}

    def search(self, query: str = "", alternatives: list[str] | None = None, limit: int = 10, offset: int = 0,
               semantic: dict | None = None, **filters) -> dict:
        where, params = _filters(**filters)
        phrasings = [terms_of(query)] + [terms_of(alt) for alt in (alternatives or [])]
        phrasings = [p for p in phrasings if p]
        scores: dict[int, float] = {}
        if not phrasings and (query or alternatives) and not semantic:
            return {"matches": 0, "results": [],
                    "note": "No searchable English words in the query. Translate it to English keywords."}
        if phrasings:
            for i, terms in enumerate(phrasings):
                penalty = 1.0 if i == 0 else ALTERNATIVE_PENALTY
                for row, score in self._score_phrasing(terms, where, params).items():
                    scores[row] = max(scores.get(row, 0.0), score * penalty)
        elif not semantic:  # no words: the most downloaded assets that pass the filters
            sql = f"SELECT a.rowid, a.popularity FROM assets a WHERE {where} ORDER BY a.popularity DESC LIMIT ?"
            for row, pop in self.conn.execute(sql, [*params, CANDIDATES]):
                scores[row] = self._popularity(pop)

        if semantic and (query or alternatives):  # by meaning too: words, text vectors, preview images
            rankings = {"keyword": [row for row, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)]}
            rankings.update(self._semantic_ranks(semantic, where, params))
            scores = self._fuse(rankings)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        rows = self._rows([row for row, _ in ranked])
        groups: dict[str, dict] = {}
        ordered: list[dict] = []
        for row, score in ranked:
            asset = rows[row]
            key = asset["variant_group"] or asset["id"]
            if key in groups:
                groups[key]["variants"] = groups[key].get("variants", 1) + 1
                continue
            item = compact(asset)
            item["score"] = round(score, 3)
            groups[key] = item
            ordered.append(item)

        result = {"matches": len(ordered), "results": ordered[offset:offset + limit]}
        unknown_size = self._unknown_size_excluded(phrasings, filters)
        if unknown_size:
            result["excluded_unknown_size"] = unknown_size
        return result

    def _rows(self, rowids: list[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for start in range(0, len(rowids), 500):
            chunk = rowids[start:start + 500]
            sql = f"SELECT rowid AS _rowid, * FROM assets WHERE rowid IN ({', '.join('?' for _ in chunk)})"
            for row in self.conn.execute(sql, chunk):
                out[row["_rowid"]] = decode(row)
        return out

    def _unknown_size_excluded(self, phrasings: list[list[str]], filters: dict) -> int:
        """How many word matches the size filter dropped because their size is unknown."""
        used = [k for k in SIZE_FILTERS if filters.get(k) is not None]
        if not used:
            return 0
        relaxed = {k: v for k, v in filters.items() if k not in SIZE_FILTERS}
        where, params = _filters(**relaxed)
        height_only = all("height" in k for k in used)
        where += " AND a.size_y IS NULL" if height_only else " AND a.max_dim_m IS NULL"
        if not phrasings:
            return self.conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0]
        query = " OR ".join(map(_quote, phrasings[0]))
        sql = (f"SELECT COUNT(*) FROM assets_fts JOIN assets a ON a.rowid = assets_fts.rowid "
               f"WHERE assets_fts MATCH ? AND {where}")
        return self.conn.execute(sql, [query, *params]).fetchone()[0]

    def get(self, asset_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            return None
        asset = decode(row)
        asset["size_m"] = size_m([asset.pop(k) for k in ("size_x", "size_y", "size_z")], digits=None)
        if asset["size_m"] is None and (asset.get("attributes") or {}).get("dimensions_m"):
            asset["dims_m"] = asset["attributes"].pop("dimensions_m")
        asset["how_to_get"] = how_to_get(asset)
        if asset["variant_group"]:
            others = [r[0] for r in self.conn.execute(
                "SELECT id FROM assets WHERE variant_group = ? AND id != ? ORDER BY rowid",
                (asset["variant_group"], asset_id))]
            asset["variants"] = others[:MAX_VARIANTS]
            if len(others) > MAX_VARIANTS:  # Megascans has 316 "Graffiti Tag"s: name a few, count the rest
                asset["variant_count"] = len(others)
        return asset


def size_m(sizes: list, digits: int | None = 3) -> list | None:
    """[width, height, depth] with unknown trailing axes dropped (materials: [width, height]).
    An unknown axis in the middle stays as None, so a known height is never read as a width."""
    out = [(round(s, digits) if digits is not None else s) if s else None for s in sizes]
    while out and out[-1] is None:
        out.pop()
    return out or None


def compact(asset: dict) -> dict:
    """The few fields an agent needs to choose, with empty ones left out to save context."""
    size = size_m([asset.get(k) for k in ("size_x", "size_y", "size_z")])
    full = {**asset, "size_m": size}
    out = {k: full.get(k) for k in COMPACT_FIELDS if full.get(k) not in (None, "", [])}
    dims = (asset.get("attributes") or {}).get("dimensions_m")
    if dims and not size:
        out["dims_m"] = dims  # metres, largest first, which axis is up unknown
    if out.get("style") == "photoreal":
        out.pop("style")  # the default; only stylized / lowpoly are worth the tokens
    if not asset.get("redistributable"):
        out["licence"] = asset.get("licence")
    return out


def how_to_get(asset: dict) -> str:
    downloads = asset.get("downloads") or {}
    if asset["source"] == "polyhaven":
        return (f"GET {downloads.get('files_api')} (send a User-Agent naming your tool). The JSON is keyed by "
                "format (models: gltf, fbx, blend, usd; textures: one key per map plus gltf/blend; HDRIs: hdri, exr), "
                "then resolution (1k, 2k, 4k, 8k). For gltf, download the .gltf url and every file under 'include', "
                "keeping the relative paths. Credit: Powered by Poly Haven.")
    if asset["source"] == "ambientcg":
        zips = downloads.get("zips") or []
        options = ", ".join(z["attributes"] for z in zips[:8])
        example = next((z for z in zips if "2K-JPG" in (z["attributes"] or "")), zips[0] if zips else None)
        line = f"Download one zip and unpack it. Options: {options}."
        if example:
            line += f" E.g. {example['attributes']}: {example['url']} ({round((example['size'] or 0) / 1e6, 1)} MB)."
        return line
    if asset["source"] in ("kenney", "quaternius"):
        kit = (asset.get("attributes") or {}).get("collection")
        if asset.get("acquire") == "local":
            return f"Part of the {kit} kit, which is on this machine: call fetch_asset."
        return (f"Part of the {kit} kit. Download the kit from {downloads.get('kit_page')}, then run "
                f"`sozai ingest {asset['source']} <folder>`; fetch_asset works after that.")
    if asset["source"] == "megascans":
        return (f"Free on Fab: open {downloads.get('fab_listing') or asset.get('url')}, sign in with an Epic account, "
                "add it to your library (free), then download it there (choose the format: FBX/glTF for "
                "models, the texture maps for surfaces) or with the Fab plugin in Unreal Engine. sozai cannot "
                "download it for you. Fab Standard License: use it in your projects, but do not redistribute "
                "the files.")
    return asset.get("url") or ""
