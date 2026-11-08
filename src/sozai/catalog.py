"""Offline catalogs for libraries without an API (Kenney, Quaternius): read kit zips, measure each model.

A maintainer runs `sozai ingest <source> <folder of kit zips>` once per library update. Each model
inside a kit becomes one record with a measured size, triangle count, rig and animation facts and
the kit's own preview image. The records go to <data dir>/catalog/<source>.jsonl and are merged into
the index by `sozai build`. The published data already lists every kit model; ingesting is only
needed to fetch them (their files come from the user's own kit zips).
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from sozai import paths
from sozai.fetch import check_archive, write_member
from sozai.measure import measure, parse_gltf

MODEL_SUFFIXES = (".glb", ".gltf", ".fbx", ".obj")
PREFERENCE = {".glb": 0, ".gltf": 1, ".fbx": 2, ".obj": 3}  # FBX before OBJ: it keeps rigs and animations
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class Library:
    """How one library names, credits and links its kits."""
    name = ""
    licence = "CC0-1.0"
    author = ""

    def kit_slug(self, zip_name: str) -> str:
        stem = Path(zip_name).stem.lower()
        return re.sub(r"[^a-z0-9]+", "-", stem).strip("-")

    def kit_title(self, slug: str) -> str:
        return slug.replace("-", " ").title()

    def kit_url(self, slug: str) -> str:
        return ""

    def credit(self, kit_title: str) -> str:
        return f"{kit_title} by {self.author}, CC0"

    def style(self, slug: str) -> str:
        return "lowpoly"


class Kenney(Library):
    name, author = "kenney", "Kenney (kenney.nl)"

    def kit_slug(self, zip_name: str) -> str:
        # kenney_city-kit-commercial_2.1.zip -> city-kit-commercial (the kit page's slug)
        stem = re.sub(r"_[0-9.]+$", "", Path(zip_name).stem)
        return super().kit_slug(stem + ".zip").removeprefix("kenney-")

    def kit_url(self, slug: str) -> str:
        return f"https://kenney.nl/assets/{slug}"


class Quaternius(Library):
    name, author = "quaternius", "Quaternius (quaternius.com)"

    def kit_slug(self, zip_name: str) -> str:
        # quaternius_animatedman_fbx.zip and quaternius_megakit_part2.zip belong to one kit each
        slug = super().kit_slug(zip_name).removeprefix("quaternius-")
        slug = re.sub(r"-(fbx|obj|gltf|glb|part-?\d+)$", "", slug)
        return re.sub(r"-(by-quaternius|quaternius|[a-z]+-20\d\d|20\d\d)$", "", slug)

    def kit_url(self, slug: str) -> str:
        return f"https://quaternius.com/packs/{slug.replace('-', '')}.html"


LIBRARIES: dict[str, Library] = {lib.name: lib for lib in (Kenney(), Quaternius())}


def _words(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^A-Za-z0-9]+", text.lower()) if w and not w.isdigit()]


def _title(stem: str) -> str:
    return " ".join(w.capitalize() for w in _words(stem)) or stem


def pick_models(names: list[str]) -> list[str]:
    """One file per model: GLB over glTF over OBJ when a kit ships several formats of the same model."""
    best: dict[str, str] = {}
    for name in names:
        path = PurePosixPath(name)
        if path.suffix.lower() not in MODEL_SUFFIXES or "__MACOSX" in path.parts or path.name.startswith("."):
            continue
        key = path.stem.lower()
        current = best.get(key)
        if current is None or PREFERENCE[path.suffix.lower()] < PREFERENCE[PurePosixPath(current).suffix.lower()]:
            best[key] = name
    return sorted(best.values())


def _basename_index(names: list[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for name in names:
        if not name.endswith("/"):
            index.setdefault(PurePosixPath(name).name.lower(), []).append(name)
    return index


def _find_elsewhere(index: dict[str, list[str]], member: str, uri: str) -> str | None:
    """A texture the model expects next to itself but the kit keeps in another folder (Textures/)."""
    candidates = index.get(PurePosixPath(uri).name.lower(), [])
    if not candidates:
        return None
    top = PurePosixPath(member).parts[0]
    return min(candidates, key=lambda c: (PurePosixPath(c).parts[0] != top, "texture" not in c.lower(), len(c)))


def _dependencies(zf: zipfile.ZipFile, member: str, index: dict[str, list[str]] | None = None,
                  missing: list[str] | None = None) -> dict[str, str]:
    """Files a .gltf, .glb or .obj points at (buffers, textures, .mtl): {zip member: where the model expects it}.

    Usually both are the same path. Some kits keep one shared Textures/ folder while their models
    point at textures beside themselves; those textures are found by name and placed where expected.
    """
    path = PurePosixPath(member)
    folder = path.parent
    wanted: list[str] = []
    geometry: set[str] = set()
    try:
        if path.suffix.lower() in (".gltf", ".glb"):  # a .glb may still point at external textures
            doc, _ = parse_gltf(zf.read(member), member)
            geometry = {b.get("uri") for b in doc.get("buffers", [])}
            wanted = [b.get("uri") for b in doc.get("buffers", []) + doc.get("images", [])]
        elif path.suffix.lower() == ".obj":
            text = zf.read(member).decode("utf-8", "replace")
            wanted = re.findall(r"^mtllib\s+(.+?)\s*$", text, re.M)
            for mtl in list(wanted):
                mtl_member = str(folder / mtl)
                if mtl_member in zf.namelist():
                    mtl_text = zf.read(mtl_member).decode("utf-8", "replace")
                    wanted += re.findall(r"^\s*map_\w+\s+(?:-\S+\s+\S+\s+)*(.+?)\s*$", mtl_text, re.M)
    except (KeyError, ValueError):
        return {}
    names = set(zf.namelist())
    out: dict[str, str] = {}
    for uri in wanted:
        if not uri or uri.startswith("data:"):
            continue
        expected = posixpath.normpath(str(folder / uri.replace("%20", " ").replace("\\", "/")))
        if expected in names:
            out[expected] = expected
        else:
            index = index if index is not None else _basename_index(list(names))
            found = _find_elsewhere(index, member, expected)
            if found and not expected.startswith("../"):
                out[found] = expected
            elif uri in geometry and missing is not None:
                missing.append(uri)  # the mesh data itself is absent: the model cannot be delivered
    return out


def extract_model(zf: zipfile.ZipFile, downloads: dict, target: Path) -> Path:
    """Write a kit model and the files it needs under target, laid out as the model expects."""
    moved = downloads.get("moved") or {}
    for name in downloads.get("files") or [downloads["member"]]:
        dest = (target / moved.get(name, name)).resolve()
        if name.endswith("/") or not dest.is_relative_to(target.resolve()):
            continue
        write_member(zf, name, dest)
    return target / downloads["member"]


LICENCE_FILE = re.compile(r"(^|/)licen[cs]e[^/]*\.(txt|md)$", re.IGNORECASE)
CC0_WORDS = ("cc0", "creative commons zero", "public domain")


def kit_licence(zf: zipfile.ZipFile, names: list[str], library: Library, kit_title: str) -> tuple[str, str]:
    """(licence, credit) for one kit, from the licence file it ships when there is one: nearly all
    are CC0, but a library may put a pack under other terms, which must then not read as CC0."""
    files = [n for n in names if LICENCE_FILE.search(n) and "__MACOSX" not in n]
    if not files:
        return library.licence, library.credit(kit_title)
    with zf.open(files[0]) as handle:
        text = handle.read(65536).decode("utf-8", "replace").lower()
    if any(word in text for word in CC0_WORDS):
        return library.licence, library.credit(kit_title)
    return "LicenseRef-kit-licence", f"{kit_title} by {library.author}; see the licence file in the kit ({files[0]})"


def _preview_for(names: list[str], model: str) -> str | None:
    stem = PurePosixPath(model).stem.lower()
    for name in names:
        path = PurePosixPath(name)
        if path.suffix.lower() in IMAGE_SUFFIXES and path.stem.lower() == stem and "texture" not in name.lower():
            return name
    return None


def _extract(zf: zipfile.ZipFile, names: Iterable[str], target: Path) -> None:
    for name in names:
        if name.endswith("/"):
            continue
        dest = target / name
        if not dest.resolve().is_relative_to(target.resolve()):
            continue  # never write outside the temporary folder
        write_member(zf, name, dest)


def ingest_zip(library: Library, zip_path: Path, previews_dir: Path, converted_dir: Path | None = None,
               log: Callable[[str], None] = lambda m: None) -> Iterable[dict]:
    slug = library.kit_slug(zip_path.name)
    kit_title = library.kit_title(slug)
    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory(prefix="sozai-") as tmp:
        check_archive(zf, zip_path.name)
        names = zf.namelist()
        index = _basename_index(names)
        models = pick_models(names)
        licence, credit = kit_licence(zf, names, library, kit_title)
        converted: dict[str, Path] = {}
        fbx = [m for m in models if m.lower().endswith(".fbx")]
        if fbx and converted_dir is not None:
            from sozai import convert

            if convert.find_blender() is None:
                log(f"sozai: {zip_path.name}: {len(fbx)} FBX models skipped (Blender not found)")
            else:  # FBX points at textures by path: give Blender the whole kit, convert in one run
                kit_root = Path(tmp) / "kit"
                _extract(zf, names, kit_root)
                out_dir = converted_dir / library.name / slug
                jobs = [(kit_root / m, out_dir / (PurePosixPath(m).stem + ".glb")) for m in fbx]
                # an earlier ingest already converted this kit with the same script: reuse its GLBs
                stamp = out_dir / ".converter"
                same = stamp.is_file() and stamp.read_text() == convert.script_version()
                fresh = {src: dst for src, dst in jobs
                         if same and dst.is_file() and dst.stat().st_mtime >= zip_path.stat().st_mtime}
                done = {**fresh, **convert.fbx_to_glb([job for job in jobs if job[0] not in fresh])}
                if done:
                    stamp.write_text(convert.script_version())
                converted = {m: done[kit_root / m] for m in fbx if kit_root / m in done}
                if len(converted) < len(fbx):
                    log(f"sozai: {zip_path.name}: {len(fbx) - len(converted)} FBX models failed to convert")
        for member in models:
            target = Path(tmp) / "m"
            shutil.rmtree(target, ignore_errors=True)
            moved: dict[str, str] = {}
            if member.lower().endswith(".fbx"):
                if member not in converted:
                    continue
                files, model_file = [member], converted[member]
            else:  # extract the model and what it references, then measure it
                lost: list[str] = []
                deps = _dependencies(zf, member, index, lost)
                if lost:
                    log(f"sozai: {zip_path.name}: skipped {member}: the kit lacks {', '.join(lost)}")
                    continue
                files = [member, *deps]
                moved = {name: where for name, where in deps.items() if name != where}
                model_file = extract_model(zf, {"member": member, "files": files, "moved": moved}, target)
            try:
                facts = measure(model_file)
            except (ValueError, KeyError, IndexError, json.JSONDecodeError):
                continue
            if not facts["triangles"]:
                continue  # no geometry: an animation-only file, an empty scene
            stem = PurePosixPath(member).stem
            preview_member = _preview_for(names, member)
            thumbnail = None
            if preview_member:
                out = previews_dir / library.name / slug / (stem + PurePosixPath(preview_member).suffix.lower())
                write_member(zf, preview_member, out)
                thumbnail = out.resolve().as_uri()
            size = facts["size"] or [None, None, None]
            words = _words(stem)
            yield {
                "id": f"{library.name}:{slug}/{stem}",
                "source": library.name,
                "source_id": f"{slug}/{stem}",
                "title": _title(stem),
                "url": library.kit_url(slug),
                "authors": [library.author.split(" (")[0]],
                "licence": licence,
                "redistributable": licence == "CC0-1.0",
                "credit": credit,
                "kind": "model",
                "style": library.style(slug),
                "creation": "modeled",
                # The kit name reaches search only through attributes.collection (the lowest-weight
                # field): in tags or category it would make every item of "Mini Characters" match
                # "character" as strongly as the characters themselves.
                "category": library.name,
                "tags": list(dict.fromkeys(words)),
                "description": "",
                "size_x": size[0], "size_y": size[1], "size_z": size[2],
                "size_source": "measured" if facts["size"] else None,
                "polycount": facts["triangles"] or None,
                "max_texture_px": facts["max_texture_px"],
                "tileable": False,
                "rigged": facts["rigged"],
                "animated": facts["animated"],
                "thumbnail_url": thumbnail,
                "acquire": "local",
                "downloads": {"kit_zip": str(zip_path.resolve()), "member": member,
                              "files": files, "kit_page": library.kit_url(slug),
                              **({"moved": moved} if moved else {}),
                              **({"converted": str(converted[member].resolve())} if member in converted else {})},
                "attributes": {"collection": kit_title, "animations": facts["animations"],
                               "format": PurePosixPath(member).suffix.lower().lstrip(".")},
                "variant_group": None,
                "popularity": None,
                "published": None,
            }


def ingest(source: str, folder: Path, log: Callable[[str], None] = print, thumbnails: bool = True) -> dict:
    library = LIBRARIES.get(source)
    if library is None:
        raise ValueError(f"unknown library {source!r}; known: {', '.join(LIBRARIES)}")
    zips = sorted(Path(folder).expanduser().glob("**/*.zip"))
    if not zips:
        raise ValueError(f"no .zip files under {folder}")
    out_dir = paths.data_dir() / "catalog"
    out_dir.mkdir(parents=True, exist_ok=True)
    previews = out_dir / "previews"
    records = []
    for zip_path in zips:
        before = len(records)
        try:
            records.extend(ingest_zip(library, zip_path, previews, out_dir / "converted", log))
        except zipfile.BadZipFile:
            log(f"sozai: skipped {zip_path.name}: not a zip")
            continue
        except Exception as error:  # one broken kit must not cost the whole ingest
            del records[before:]
            log(f"sozai: skipped {zip_path.name}: {error}")
            continue
        log(f"sozai: {zip_path.name}: {len(records) - before} models")
    # a kit split over several zips (an OBJ zip and an FBX zip, part1/part2): keep the best format per model
    best: dict[str, dict] = {}
    for record in records:
        current = best.get(record["id"])
        rank = PREFERENCE["." + record["attributes"]["format"]]
        if current is None or rank < PREFERENCE["." + current["attributes"]["format"]]:
            best[record["id"]] = record
    records = list(best.values())
    from sozai.scale import calibrate

    scale = calibrate(records, source)
    rendered = None
    if thumbnails:
        from sozai.thumbs import render_missing

        rendered = render_missing(records, previews, log)
    target = out_dir / f"{source}.jsonl"
    tmp = target.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    tmp.replace(target)
    return {"source": source, "kits": len(zips), "models": len(records), "catalog": str(target),
            "calibrated_kits": len(scale["calibrated"]), "uncalibrated_kits": scale["uncalibrated"],
            **({"previews": rendered} if rendered else {})}


def load_catalogs(directory: Path | None = None) -> dict[str, list[dict]]:
    """Every <source>.jsonl in the catalog folder, by source name."""
    directory = directory or paths.data_dir() / "catalog"
    out: dict[str, list[dict]] = {}
    for file in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            local = (row.get("downloads") or {}).get("kit_zip")
            if row.get("acquire") == "local" and local and not Path(local).is_file():
                row["acquire"] = "human"  # the kit zip moved away: the user has to get the kit again
        out[file.stem] = rows
    return out
