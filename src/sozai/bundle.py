"""The published data: every library's records plus their vectors, as one GitHub Release attachment.

Maintainers: `sozai pack` writes dist/sozai-data-<version>.zip from the local index and vectors.
Users: `sozai build` downloads that zip once (DATA_URL) and builds the index from it, with no
calls to the libraries' APIs; `sozai build --live` reads the APIs instead, for the newest assets.

The zip holds no asset files and no previews, only facts about assets: what they are, their size,
licence and where to get them. Paths on the maintainer's machine are removed: kit models become
"download the kit from its page" until the user ingests the kit themselves.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from sozai import __version__, index, paths, semantic

DATA_VERSION = "2026.11.09"
DATA_URL = f"https://github.com/kanaworksai-hue/sozai-search-mcp/releases/download/data-{DATA_VERSION}/sozai-data-{DATA_VERSION}.zip"
RECORDS = "records.jsonl.gz"
VECTORS = "vectors.npz"
MANIFEST = "manifest.json"
NOTICE = "NOTICE.txt"
NOTICE_TEXT = """\
Sozai Search MCP published data

This archive lists facts about free 3D assets: what each asset is, its size, licence and where to
get it, plus search vectors computed by Sozai Search. It contains no asset files and no previews.

- Every record names the licence of the asset it describes (field "licence"). Assets are always
  downloaded from their own library, under that library's terms.
- Poly Haven records come from the Poly Haven API (https://polyhaven.com), used under its API
  terms. Poly Haven keeps its rights in that data. Powered by Poly Haven.
- ambientCG records come from the ambientCG API (https://ambientcg.com); assets CC0.
- Kenney and Quaternius records were measured from their CC0 kits by Sozai Search.
- KayKit (Kay Lousberg, github.com/KayKit-Game-Assets) and Polygonal Mind (GLB versions at
  github.com/ToxSam/cc0-models-Polygonal-Mind) records were measured from their CC0 files on GitHub.
- Open Source Avatars records (https://www.opensourceavatars.com, registry at
  github.com/ToxSam/open-source-avatars) were measured from the CC0 VRM files on Arweave.
- NASA 3D Resources records were measured from the files at github.com/nasa/NASA-3D-Resources,
  with descriptions and preview links from science.nasa.gov/3d-resources. Courtesy NASA; used
  under NASA's media usage guidelines (https://www.nasa.gov/nasa-brand-center/images-and-media/).
- Quixel Megascans records describe listings on Fab (https://www.fab.com). The assets
  themselves are on Fab under the Fab Standard License and are never included.
- Sozai Search's own fields (measured sizes, scale calibration, search text) and the vectors
  are released under the MIT licence, like its code.

Not affiliated with or endorsed by Epic Games, Quixel, Poly Haven, ambientCG, Kenney, Quaternius,
KayKit, Polygonal Mind, Open Source Avatars or NASA.
"""
KIT_SOURCES = ("kenney", "quaternius")


def bundle_dir() -> Path:
    return paths.data_dir() / "bundle"


def _public(record: dict) -> dict:
    """A record without anything that only exists on the maintainer's machine."""
    record = dict(record)
    if record["source"] in KIT_SOURCES:
        downloads = record.get("downloads") or {}
        record["downloads"] = {"kit_page": downloads.get("kit_page"), "member": downloads.get("member")}
        record["acquire"] = "human"
    if str(record.get("thumbnail_url") or "").startswith("file:"):
        record["thumbnail_url"] = None
    attributes = dict(record.get("attributes") or {})
    attributes.pop("thumbnail", None)
    record["attributes"] = attributes
    record.pop("max_dim_m", None)
    if record["source"] == "megascans":  # the credit line this release of sozai gives them
        from sozai.sources.megascans import CREDIT

        record["credit"] = CREDIT
    return record


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def pack(out_dir: Path | None = None, version: str = DATA_VERSION, log: Callable[[str], None] = print) -> dict:
    out_dir = Path(out_dir or "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = index.connect(paths.index_path())
    try:
        records = [_public(index.decode(row)) for row in conn.execute("SELECT * FROM assets ORDER BY id")]
    finally:
        conn.close()
    work = out_dir / f"sozai-data-{version}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    with gzip.open(work / RECORDS, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    (work / NOTICE).write_text(NOTICE_TEXT, encoding="utf-8")
    files = [RECORDS, NOTICE]
    if semantic.vectors_path().is_file():
        shutil.copyfile(semantic.vectors_path(), work / VECTORS)
        files.append(VECTORS)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["source"]] = counts.get(record["source"], 0) + 1
    manifest = {"data_version": version, "made_with": f"sozai-search-mcp {__version__}",
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "assets": len(records),
                "sources": counts, "embedding_model": semantic.MODEL_ID, "embedding_dim": semantic.DIM,
                "files": {name: _sha256(work / name) for name in files}}
    (work / MANIFEST).write_text(json.dumps(manifest, indent=1))
    archive = out_dir / f"sozai-data-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:  # the parts are compressed already
        for name in [MANIFEST, *files]:
            zf.write(work / name, name)
    shutil.rmtree(work)
    log(f"sozai: wrote {archive} ({archive.stat().st_size / 1e6:.1f} MB, {len(records)} assets)")
    return {"path": str(archive), "bytes": archive.stat().st_size, **manifest}


def install(source: str = DATA_URL, log: Callable[[str], None] = print) -> dict:
    """Download (or copy) a data zip, check it and unpack it into the data folder."""
    downloaded = source.startswith("https://")
    if downloaded:
        from sozai.fetch import download

        log(f"sozai: downloading {source}")
        archive = download(source)
    else:
        archive = Path(source).expanduser()
    try:
        with paths.data_lock():  # two installs at once would delete each other's folder
            return _unpack(archive, log)
    except (zipfile.BadZipFile, ValueError, KeyError):
        if downloaded:  # never reuse a bad download from the cache: the next try fetches it again
            archive.unlink(missing_ok=True)
        raise


def _unpack(archive: Path, log: Callable[[str], None]) -> dict:
    target = bundle_dir()
    tmp = target.with_name(f"bundle.tmp-{os.getpid()}")  # two sessions may install at once
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        return _unpack_into(archive, tmp, target, log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _unpack_into(archive: Path, tmp: Path, target: Path, log: Callable[[str], None]) -> dict:
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        if MANIFEST not in names:
            raise ValueError(f"{archive} is not a sozai data zip")
        manifest = json.loads(zf.read(MANIFEST))
        for name in manifest["files"]:
            if "/" in name or "\\" in name or name not in names:
                raise ValueError(f"bad file in data zip: {name}")
            zf.extract(name, tmp)
            if _sha256(tmp / name) != manifest["files"][name]:
                raise ValueError(f"{name} is damaged (checksum mismatch)")
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=1))
    shutil.rmtree(target, ignore_errors=True)
    tmp.replace(target)
    if (target / VECTORS).is_file():
        shutil.copyfile(target / VECTORS, semantic.vectors_path())
    log(f"sozai: data {manifest['data_version']}: {manifest['assets']} assets")
    return manifest


def installed() -> dict | None:
    manifest = bundle_dir() / MANIFEST
    return json.loads(manifest.read_text()) if manifest.is_file() else None


def _date(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return None


def outdated(manifest: dict | None) -> bool:
    """True when this sozai release expects newer published data than the installed one.

    Versions are dates (2026.11.08). Data the user installed themselves with another kind of
    version (`sozai build --data my.zip`) is never replaced, and neither is data newer than ours.
    """
    if manifest is None:
        return True
    have, want = _date(manifest.get("data_version", "")), _date(DATA_VERSION)
    return have is not None and want is not None and have < want


def load_records() -> list[dict]:
    """The published records, cleaned again on the way in: whatever the zip says, records from it
    never point at files on this machine (only `sozai ingest` writes those, into the catalog)."""
    with gzip.open(bundle_dir() / RECORDS, "rt", encoding="utf-8") as handle:
        return [_public(json.loads(line)) for line in handle if line.strip()]
