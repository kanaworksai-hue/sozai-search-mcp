"""Get an asset's files into a project folder, ready to use.

- quality picks the texture resolution: web 1k, game 2k, film 4k (or pass resolution directly).
- Models become one self-contained .glb by default (format="gltf" keeps the separate files).
- Materials become a folder of PBR maps: Poly Haven adds material.json (maps and the real-world tile
  size), ambientCG ships ready-made .tres (Godot), .mtlx (MaterialX) and .blend materials.
- HDRIs: .hdr for web/game, .exr for film.
- The credit line is appended to CREDITS.md in the destination folder.
Downloads are cached under the data folder, so fetching the same file twice costs nothing.

Names and URLs come from library APIs and the published data, so every path built from them is
checked to stay inside the destination folder, and the work happens in a private staging folder:
sozai only ever adds or replaces its own output files, it never deletes anything that was there.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from sozai import paths
from sozai.http import Http, UnsafeURL, check_url, open_url
from sozai.protocol import ToolError

RESOLUTION = {"web": "1k", "game": "2k", "film": "4k"}
MAX_DOWNLOAD = 8 << 30          # bytes; the largest real files (8k EXRs, ambientCG zips) are far below
MAX_UNPACKED = 16 << 30         # bytes a zip may expand to
MAX_MEMBERS = 20_000            # files a zip may hold
MAP_KEYS = {  # Poly Haven texture map keys, by the name sozai gives the map
    "color": ("diffuse", "diff", "col", "color", "albedo"),
    "normal": ("nor_gl",),
    "roughness": ("rough", "roughness"),
    "arm": ("arm",),
    "ao": ("ao",),
    "displacement": ("disp", "displacement"),
    "metalness": ("metal", "metalness"),
}


def _safe_name(asset_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(asset_id).replace(":", "_")).strip("_.")
    return name or "file"


def inside(base: Path, rel: str | Path) -> Path:
    """base/rel, refusing anything that would land outside base (absolute paths, '..', drive letters)."""
    rel = str(rel).replace("\\", "/")
    target = (base / rel).resolve()
    if PurePosixPath(rel).is_absolute() or re.match(r"^[A-Za-z]:", rel) or not target.is_relative_to(base.resolve()):
        raise ToolError(f"unsafe path from the asset data: {rel}")
    return target


def _url_name(url: str) -> str:
    return _safe_name(unquote(PurePosixPath(urlparse(url).path).name))


def download(url: str, expected_md5: str | None = None, max_bytes: int = MAX_DOWNLOAD) -> Path:
    """Fetch a URL into the cache (once) and return the cached file."""
    try:
        check_url(url)  # before touching the cache folder
    except UnsafeURL as error:
        raise ToolError(str(error)) from None
    parsed = urlparse(url)
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    name = (parse_qs(parsed.query).get("file") or [""])[0]  # ambientCG: /get?file=<name>.zip
    name = _safe_name(PurePosixPath(unquote(name).replace("\\", "/")).name) if name else _url_name(url)
    folder = paths.data_dir() / "cache" / digest
    target = inside(folder, name)
    if target.is_file():
        return target
    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.part")  # two sessions may fetch the same file
    md5 = hashlib.md5()
    size = 0
    try:
        with open_url(url, timeout=60) as response, tmp.open("wb") as out:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise ToolError(f"{url} is larger than {max_bytes >> 20} MB; not downloaded")
                md5.update(chunk)
                out.write(chunk)
        if expected_md5 and md5.hexdigest() != expected_md5:
            raise ToolError(f"checksum mismatch for {url}")
        tmp.replace(target)
    except UnsafeURL as error:
        raise ToolError(str(error)) from None
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _place(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def check_archive(zf: zipfile.ZipFile, label: str) -> None:
    """Refuse archives that would unpack to absurd sizes or file counts (zip bombs)."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise ToolError(f"{label} is too large to unpack safely")


def write_member(zf: zipfile.ZipFile, member: str, dest: Path) -> Path:
    """Stream one archive member to dest (zipfile stops at the size the archive declares)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    return dest


def _extract(zf: zipfile.ZipFile, member: str, folder: Path, as_name: str | None = None) -> Path:
    try:
        dest = inside(folder, as_name or member)
    except ToolError:
        raise ToolError(f"unsafe path in archive: {member}") from None
    return write_member(zf, member, dest)


def _credit(dest: Path, asset: dict) -> None:
    line = f"- {asset['title']} ({asset.get('url') or asset['id']}): {asset.get('credit') or asset['licence']}\n"
    credits = dest / "CREDITS.md"
    text = credits.read_text(encoding="utf-8") if credits.exists() else "# Asset credits\n\n"
    if line not in text:
        credits.write_text(text + line, encoding="utf-8")


# --- per source ---

def _polyhaven(asset: dict, folder: Path, resolution: str, fmt: str, quality: str) -> tuple[Path, list[Path], list[str]]:
    files = Http().get_json(asset["downloads"]["files_api"])
    notes: list[str] = []
    name = _safe_name(asset["source_id"])

    def pick_res(options: dict) -> str:
        if resolution in options:
            return resolution
        order = sorted(options, key=lambda r: int(re.sub(r"\D", "", r) or 0))
        smaller = [r for r in order if int(re.sub(r"\D", "", r) or 0) <= int(re.sub(r"\D", "", resolution) or 0)]
        chosen = (smaller or order)[-1]
        notes.append(f"{resolution} not offered; used {chosen}")
        return chosen

    if asset["kind"] == "model":
        res = pick_res(files["gltf"])
        entry = files["gltf"][res]["gltf"]
        work = inside(folder, _safe_name(f"{name}_{res}"))
        gltf = _place(download(entry["url"], entry.get("md5")), inside(work, _url_name(entry["url"])))
        placed = [gltf]
        for rel, inc in entry.get("include", {}).items():  # textures and buffers, where the .gltf expects them
            placed.append(_place(download(inc["url"], inc.get("md5")), inside(work, rel)))
        if fmt == "glb":
            from sozai.gltf_pack import pack

            glb = pack(gltf, inside(folder, _safe_name(f"{name}_{res}") + ".glb"))
            shutil.rmtree(work)
            return glb, [glb], notes
        return gltf, placed, notes

    if asset["kind"] == "hdri":
        res = pick_res(files["hdri"])
        ext = "exr" if quality == "film" else "hdr"
        entry = files["hdri"][res].get(ext) or next(iter(files["hdri"][res].values()))
        out = _place(download(entry["url"], entry.get("md5")), inside(folder, _url_name(entry["url"])))
        return out, [out], notes

    # material: one file per PBR map
    lowered = {k.lower(): k for k in files}
    work = inside(folder, f"{name}_{resolution}")
    maps: dict[str, str] = {}
    placed: list[Path] = []
    for role, keys in MAP_KEYS.items():
        key = next((lowered[k] for k in keys if k in lowered), None)
        if not key:
            continue
        res = pick_res(files[key])
        options = files[key][res]
        entry = options.get("jpg") or options.get("png") or next(iter(options.values()))
        out = _place(download(entry["url"], entry.get("md5")), inside(work, _url_name(entry["url"])))
        maps[role] = out.name
        placed.append(out)
    size = asset.get("size_m")
    meta = {"asset": asset["id"], "maps": maps, "normal": "OpenGL (Y+)",
            "tile_size_m": size[:2] if size else None, "credit": asset.get("credit")}
    material = work / "material.json"
    material.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return material, [material, *placed], notes


def _ambientcg(asset: dict, folder: Path, resolution: str, fmt: str, quality: str) -> tuple[Path, list[Path], list[str]]:
    zips = (asset.get("downloads") or {}).get("zips") or []
    want = resolution.upper()
    notes: list[str] = []
    choice = next((z for z in zips if z["attributes"] in (f"{want}-JPG", f"HQ-{want}-JPG", f"LQ-{want}-JPG")), None)
    if choice is None:
        choice = next((z for z in zips if want in (z["attributes"] or "")), None) or (zips[0] if zips else None)
        if choice is None:
            raise ToolError(f"{asset['id']} has no downloads")
        notes.append(f"{want} JPG not offered; used {choice['attributes']}")
    work = inside(folder, _safe_name(f"{asset['source_id']}_{choice['attributes']}"))
    placed = []
    with zipfile.ZipFile(download(choice["url"])) as zf:
        check_archive(zf, choice["url"])
        for member in zf.namelist():
            if not member.endswith("/"):
                placed.append(_extract(zf, member, work))
    main = next((p for p in placed if p.suffix.lower() in (".obj", ".glb", ".gltf")), None)
    if main is None:
        main = next((p for p in placed if p.suffix.lower() == ".tres"), None) or work
        if asset["kind"] in ("material", "decal", "atlas"):
            notes.append("maps plus ready-made .tres (Godot), .mtlx (MaterialX) and .blend materials")
    elif main.suffix.lower() == ".obj" and fmt == "glb":
        notes.append("ambientCG ships this model as OBJ (with USD); kept as OBJ")
    return main, placed, notes


def _kit(asset: dict, folder: Path, resolution: str, fmt: str, quality: str) -> tuple[Path, list[Path], list[str]]:
    main, placed, notes = _kit_files(asset, folder, fmt)
    # Kits not modelled in metres: write the calibration into the file, so the model arrives at
    # the size search reported (a 3.6-unit knight becomes 1.96 m).
    factor = ((asset.get("attributes") or {}).get("scale") or {}).get("factor")
    if factor and abs(factor - 1) > 0.001:
        if main.suffix.lower() in (".glb", ".gltf"):
            from sozai.gltf_pack import rescale

            rescale(main, factor)
            notes.append(f"scaled by {factor} to real size ({asset['attributes']['scale']['reference']})")
        else:
            notes.append(f"not rescaled: multiply this {main.suffix} by {factor} for real size in metres")
    elif factor is None and (asset.get("attributes") or {}).get("size_units"):
        notes.append("this kit has no real-world scale reference; size it by eye")
    return main, placed, notes


def _kit_files(asset: dict, folder: Path, fmt: str) -> tuple[Path, list[Path], list[str]]:
    downloads = asset.get("downloads") or {}
    kit_zip = downloads.get("kit_zip")
    if not kit_zip or not Path(kit_zip).is_file():
        raise ToolError(f"{asset['id']} comes from the {(asset.get('attributes') or {}).get('collection')} kit, which is not "
                        f"on this machine. Download the kit from {downloads.get('kit_page')} and run "
                        f"`sozai ingest {asset['source']} <folder>`.")
    member = downloads["member"]
    stem = _safe_name(asset["source_id"])
    converted = downloads.get("converted")
    catalog_dir = (paths.data_dir() / "catalog").resolve()
    if converted and Path(converted).is_file():  # an FBX the ingest already turned into a .glb
        if not Path(converted).resolve().is_relative_to(catalog_dir):  # ingest only writes there
            raise ToolError(f"{asset['id']}: converted model outside the sozai data folder; run `sozai ingest` again")
        out = _place(Path(converted), inside(folder, f"{stem}.glb"))
        return out, [out], ["converted from FBX with Blender"]
    work = inside(folder, stem)
    from sozai.catalog import extract_model

    with zipfile.ZipFile(kit_zip) as zf:
        check_archive(zf, kit_zip)
        main = extract_model(zf, downloads, work).resolve()
    moved = downloads.get("moved") or {}
    placed = [work / moved.get(name, name) for name in downloads.get("files") or [member]]
    if fmt == "glb" and main.suffix.lower() in (".gltf", ".glb"):
        from sozai.gltf_pack import needs_packing, pack

        out = inside(folder, f"{stem}.glb")
        if needs_packing(main):  # .gltf, or a .glb pointing at external textures (most Kenney kits)
            pack(main, out)
        else:
            shutil.move(main, out)
        shutil.rmtree(work)
        return out, [out], []
    return main, placed, []


HANDLERS = {"polyhaven": _polyhaven, "ambientcg": _ambientcg, "kenney": _kit, "quaternius": _kit}


def _publish(stage: Path, folder: Path) -> None:
    """Move every file sozai produced in stage to the same place under folder, replacing only
    files of the same name; whatever else is in folder stays as it is."""
    for src in sorted(p for p in stage.rglob("*") if p.is_file() and not p.is_symlink()):
        dest = inside(folder, src.relative_to(stage).as_posix())
        if dest.is_dir():
            raise ToolError(f"{dest} is a folder; not replacing it with a file")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)


def fetch_asset(asset: dict, dest: str, quality: str = "game", resolution: str | None = None, fmt: str = "glb") -> dict:
    folder = Path(dest).expanduser()
    if not folder.is_absolute():  # an MCP server's working directory is not the user's project
        raise ToolError(f"dest must be an absolute folder path, got {dest!r}")
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    resolution = (resolution or RESOLUTION[quality]).lower()
    handler = HANDLERS.get(asset["source"])
    if asset["source"] == "megascans":
        from sozai.search import how_to_get

        raise ToolError(f"{asset['id']} is a Megascans asset, which only its owner's Fab account can download. "
                        + how_to_get(asset))
    if handler is None:
        raise ToolError(f"fetching from {asset['source']} is not supported yet; see {asset.get('url')}")
    stage = Path(tempfile.mkdtemp(prefix=".sozai-", dir=folder)).resolve()
    try:
        main, placed, notes = handler(asset, stage, resolution, fmt, quality)
        _publish(stage, folder)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    def final(path: Path) -> Path:
        path = Path(path).resolve()
        return folder / path.relative_to(stage) if path.is_relative_to(stage) else path

    main, placed = final(main), [final(p) for p in placed]
    _credit(folder, asset)
    return {
        "asset_id": asset["id"],
        "main_file": str(main),
        "files": [p.relative_to(folder).as_posix() if p.is_relative_to(folder) else str(p) for p in placed],
        "folder": str(folder),
        "size_m": asset.get("size_m"),
        "licence": asset["licence"],
        "credit": asset.get("credit"),
        "notes": notes + ["credit line added to CREDITS.md"],
    }
