"""The SQLite index: schema, building it from source records, and opening it read-only.

One row per asset in `assets`, plus an FTS5 table over the text an agent searches. Every source
produces records with the fields in `COLUMNS`; list and dict fields are stored as JSON.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

SCHEMA_VERSION = 1

# (name, SQL type). Order matters: it is the column order of the assets table.
COLUMNS: list[tuple[str, str]] = [
    ("id", "TEXT PRIMARY KEY"),      # "<source>:<source_id>"
    ("source", "TEXT NOT NULL"),     # polyhaven, ambientcg, ...
    ("source_id", "TEXT NOT NULL"),
    ("title", "TEXT NOT NULL"),
    ("url", "TEXT"),                 # the asset's page on its library
    ("authors", "TEXT"),             # JSON list
    ("licence", "TEXT NOT NULL"),    # SPDX id, e.g. CC0-1.0
    ("redistributable", "INTEGER NOT NULL"),  # may the files be shipped inside a project
    ("credit", "TEXT"),              # suggested credit line
    ("kind", "TEXT NOT NULL"),       # model, material, hdri, decal, atlas, substance, terrain, brush
    ("style", "TEXT"),               # photoreal, stylized, lowpoly
    ("creation", "TEXT"),            # scanned, procedural, modeled, photographed, approximated
    ("category", "TEXT"),            # the library's category path
    ("tags", "TEXT"),                # JSON list
    ("description", "TEXT"),
    ("size_x", "REAL"),              # real-world size in metres: width; materials: covered width
    ("size_y", "REAL"),              # height (the up axis); materials: covered height
    ("size_z", "REAL"),              # depth
    ("max_dim_m", "REAL"),           # largest of the sizes, used by size filters
    ("size_source", "TEXT"),         # api, listing, measured
    ("polycount", "INTEGER"),
    ("max_texture_px", "INTEGER"),
    ("maps", "TEXT"),                # JSON list of PBR maps when known
    ("tileable", "INTEGER"),         # 1, 0 or NULL (unknown)
    ("rigged", "INTEGER"),
    ("animated", "INTEGER"),
    ("thumbnail_url", "TEXT"),
    ("acquire", "TEXT NOT NULL"),    # direct, zip, human (needs a person, e.g. a Fab claim)
    ("downloads", "TEXT"),           # JSON, source-specific: how to get the files
    ("attributes", "TEXT"),          # JSON dict of extra source facts (condition, time of day...)
    ("variant_group", "TEXT"),       # rows sharing it are variants of one asset
    ("popularity", "REAL"),          # the library's download count, when it publishes one
    ("published", "TEXT"),           # ISO date
]
COLUMN_NAMES = [name for name, _ in COLUMNS]
JSON_COLUMNS = {"authors", "tags", "maps", "downloads", "attributes"}
BOOL_COLUMNS = {"redistributable", "tileable", "rigged", "animated"}

# FTS columns and their bm25 weights (title matters most, then tags, category, description, extra).
FTS_COLUMNS = ["title", "tags", "category", "description", "extra"]
FTS_WEIGHTS = (10.0, 5.0, 3.0, 1.0, 1.0)


def _ddl() -> str:
    cols = ",\n  ".join(f"{name} {kind}" for name, kind in COLUMNS)
    return f"""
CREATE TABLE assets (
  {cols}
);
CREATE INDEX assets_kind ON assets(kind);
CREATE INDEX assets_source ON assets(source);
CREATE INDEX assets_size ON assets(max_dim_m);
CREATE VIRTUAL TABLE assets_fts USING fts5({", ".join(FTS_COLUMNS)}, tokenize='porter unicode61');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _fts_extra(record: dict) -> str:
    """Words worth matching that are not in title, tags, category or description."""
    words = [record.get("kind"), record.get("style"), record.get("creation"), record.get("source")]
    for value in (record.get("attributes") or {}).values():
        if isinstance(value, list):
            words.extend(str(v) for v in value)
        elif isinstance(value, str):
            words.append(value)
    return " ".join(str(w).replace("_", " ") for w in words if w)


def normalise(record: dict) -> dict:
    """Fill derived fields and check the record has what the schema needs."""
    for required in ("id", "source", "source_id", "title", "licence", "kind", "acquire"):
        if not record.get(required):
            raise ValueError(f"record {record.get('id')!r} lacks {required}")
    for key in ("size_x", "size_y", "size_z"):
        value = record.get(key)
        record[key] = round(value, 4) if isinstance(value, (int, float)) and value > 0 else None
    known = [record[k] for k in ("size_x", "size_y", "size_z") if record[k]]
    # Sizes whose axes are not known (Megascans listings): only the largest one feeds the size filters
    unordered = [d for d in (record.get("attributes") or {}).get("dimensions_m") or [] if d]
    record["max_dim_m"] = max(known or unordered) if known or unordered else None
    record.setdefault("redistributable", record["licence"] == "CC0-1.0")
    record["title"] = " ".join(record["title"].split())
    return record


def build(records: Iterable[dict], path: Path, sources: dict[str, int] | None = None) -> int:
    """Write a fresh index to `path` atomically (readers keep the old file until the swap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    count = 0
    try:
        conn.executescript(_ddl())
        placeholders = ", ".join("?" for _ in COLUMN_NAMES)
        seen: set[str] = set()
        for record in records:
            record = normalise(record)
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            values = []
            for name in COLUMN_NAMES:
                value = record.get(name)
                if name in JSON_COLUMNS and value is not None:
                    value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                elif name in BOOL_COLUMNS and value is not None:
                    value = int(bool(value))
                values.append(value)
            cur = conn.execute(f"INSERT INTO assets ({', '.join(COLUMN_NAMES)}) VALUES ({placeholders})", values)
            conn.execute(
                f"INSERT INTO assets_fts (rowid, {', '.join(FTS_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?)",
                (cur.lastrowid, record["title"], " ".join(record.get("tags") or []),
                 (record.get("category") or "").replace("/", " "), record.get("description") or "",
                 _fts_extra(record)))
            count += 1
        meta = {"schema_version": SCHEMA_VERSION, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "count": count, "sources": sources or {}}
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                         [(k, json.dumps(v)) for k, v in meta.items()])
        conn.execute("INSERT INTO assets_fts(assets_fts) VALUES ('optimize')")
        conn.commit()
    finally:
        conn.close()
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:  # Windows: another sozai session has the index open right now
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"{path} is in use by another sozai session; close it and try again") from None
            time.sleep(0.5)
    return count


def connect(path: Path) -> sqlite3.Connection:
    """Open the index read-only."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)  # as_uri escapes # ? %
    conn.row_factory = sqlite3.Row
    return conn


def read_meta(conn: sqlite3.Connection) -> dict:
    return {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT key, value FROM meta")}


def decode(row: sqlite3.Row) -> dict:
    """A stored row as a plain dict with JSON and boolean columns decoded."""
    out = {}
    for name in COLUMN_NAMES:
        value = row[name]
        if name in JSON_COLUMNS and value is not None:
            value = json.loads(value)
        elif name in BOOL_COLUMNS and value is not None:
            value = bool(value)
        out[name] = value
    return out
