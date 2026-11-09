"""Libraries published as public GitHub repositories: KayKit and Polygonal Mind (both CC0).

Maintainers run `sozai ingest kaykit` / `sozai ingest polygonalmind`. Nothing is cloned: the file
list comes from the GitHub API (two calls per repository) and each model is measured from its glTF
JSON alone, read from raw.githubusercontent.com (for a .glb only the JSON chunk, with a Range
request). Every record pins the commit it was read at, so its download links keep pointing at the
same files when a repository changes later. Models that ship without a preview get one rendered
with Blender, from their files downloaded into the cache.

Users get the records with the published data; fetch_asset downloads a model's files from GitHub
and packs them into one .glb.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.error
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from sozai import paths
from sozai.catalog import _title, _words
from sozai.http import Http
from sozai.measure import measure_doc, parse_gltf

API = "https://api.github.com/repos"
RAW = "https://raw.githubusercontent.com"
HEAD_BYTES = 1 << 16  # a GLB's JSON chunk is usually far smaller than this
# Colour words that tell variants of one model apart (building_tavern_blue / _red / _green).
COLOURS = {"blue", "green", "red", "yellow", "neutral", "brown", "white", "black", "purple", "orange",
           "pink", "grey", "gray", "cyan", "teal", "gold", "silver", "dark", "light"}


def raw_url(repo: str, commit: str, path: str) -> str:
    return f"{RAW}/{repo}/{commit}/{quote(path)}"


def _rate_limited(error: Exception) -> bool:
    return isinstance(error, urllib.error.HTTPError) and error.code in (403, 429)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=600).stdout


def _commit(http: Http, repo: str) -> str:
    try:
        return http.get_json(f"{API}/{repo}/commits/HEAD")["sha"]
    except Exception as error:  # the API allows 60 calls an hour without a token; git has no such limit
        if not _rate_limited(error) or not shutil.which("git"):
            raise
        return _git("ls-remote", f"https://github.com/{repo}", "HEAD").split()[0]


def _tree(http: Http, repo: str, commit: str) -> list[str]:
    try:
        data = http.get_json(f"{API}/{repo}/git/trees/{commit}?recursive=1")
    except Exception as error:
        if not _rate_limited(error) or not shutil.which("git"):
            raise
        with tempfile.TemporaryDirectory(prefix="sozai-git-") as tmp:  # file names only, no file contents
            _git("clone", "--quiet", "--bare", "--filter=blob:none", "--depth", "1",
                 f"https://github.com/{repo}", tmp)
            return _git("-C", tmp, "ls-tree", "-r", "--name-only", commit).splitlines()
    if data.get("truncated"):
        raise RuntimeError(f"{repo}: the file list is too long for one GitHub API call")
    return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]


def gltf_json(http: Http, url: str) -> dict:
    """The glTF JSON of a .gltf, or of a .glb from its first bytes only."""
    if not urlparse(url).path.lower().endswith(".glb"):
        return parse_gltf(http.get(url))[0]
    head = http.get(url, {"Range": f"bytes=0-{HEAD_BYTES - 1}"})  # a server ignoring Range sends it all
    if head[:4] != b"glTF" or len(head) < 20:
        raise ValueError(f"{url}: not a GLB")
    length = struct.unpack("<I", head[12:16])[0]
    if len(head) < 20 + length:
        head += http.get(url, {"Range": f"bytes={len(head)}-{19 + length}"})
    return json.loads(head[20:20 + length])


def _elsewhere(by_name: dict[str, list[str]], expected: str) -> str | None:
    """A file the model expects next to it but the repository keeps in another folder: the copy
    sharing the longest folder prefix with where it was expected."""
    options = by_name.get(PurePosixPath(expected).name.lower()) or []
    return max(options, key=lambda p: (len(posixpath.commonprefix([p, expected])), -len(p)), default=None)


def dependencies(doc: dict, model: str, files: set[str], by_name: dict[str, list[str]]) -> dict[str, str] | None:
    """{path the model expects: path in the repository} for its external buffers and images, or
    None when one of them is missing from the repository."""
    folder = posixpath.dirname(model)
    out: dict[str, str] = {}
    for item in doc.get("buffers", []) + doc.get("images", []):
        uri = item.get("uri")
        if not uri or uri.startswith("data:"):
            continue
        expected = posixpath.normpath(posixpath.join(folder, unquote(uri)))
        if expected.startswith(("../", "/")):
            return None
        actual = expected if expected in files else _elsewhere(by_name, expected)
        if actual is None:
            return None
        out[expected] = actual
    return out


def name_words(stem: str) -> list[str]:
    """Words of a file name: PalmTree02_Art -> Palm, Tree, 02 (a trailing "Art" is a Unity naming habit)."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]
    return words[:-1] if len(words) > 1 and words[-1].lower() == "art" else words


def _tag_words(stem: str) -> list[str]:
    return [w.lower() for w in name_words(stem) if not w.isdigit() and len(w) > 1]


def title_of(stem: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in name_words(stem)) or stem


def _by_name(files: list[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in files:
        index.setdefault(PurePosixPath(path).name.lower(), []).append(path)
    return index


def _record(source: str, author: str, credit: str, pack: str, pack_title: str, stem: str, page: str,
            repo: str, commit: str, model: str, deps: dict[str, str], doc: dict, tags: list[str],
            thumbnail: str | None, attributes: dict | None = None) -> dict | None:
    facts = measure_doc(doc)
    if not facts["triangles"]:
        return None  # no geometry: an animation-only file, an empty scene
    size = facts["size"] or [None, None, None]
    return {
        "id": f"{source}:{pack}/{stem}",
        "source": source,
        "source_id": f"{pack}/{stem}",
        "title": title_of(stem),
        "url": page,
        "authors": [author],
        "licence": "CC0-1.0",
        "redistributable": True,
        "credit": credit,
        "kind": "model",
        "style": "lowpoly",
        "creation": "modeled",
        "category": source,  # the pack name is in attributes.collection (see catalog.ingest_zip)
        "tags": list(dict.fromkeys(tags)),
        "description": "",
        "size_x": size[0], "size_y": size[1], "size_z": size[2],
        "size_source": "measured" if facts["size"] else None,
        "polycount": facts["triangles"] or None,
        "max_texture_px": None,
        "tileable": False,
        "rigged": facts["rigged"],
        "animated": facts["animated"],
        "thumbnail_url": thumbnail,
        "acquire": "direct",
        "downloads": {"repo": f"https://github.com/{repo}", "commit": commit, "main": model,
                      "files": {model: raw_url(repo, commit, model),
                                **{want: raw_url(repo, commit, have) for want, have in deps.items()}}},
        "attributes": {"collection": pack_title, "animations": facts["animations"],
                       "format": PurePosixPath(model).suffix.lower().lstrip("."), **(attributes or {})},
        "variant_group": None,
        "popularity": None,
        "published": None,
    }


def _measure_all(jobs: list, work: Callable, log: Callable[[str], None], label: str) -> list[dict]:
    """Run work(job) -> record | None for every job, a few at a time; a broken model is skipped."""
    out: list[dict] = []

    def one(job):
        try:
            return work(job)
        except Exception as error:  # one broken model must not cost the others
            log(f"sozai: {label}: skipped {job[-1] if isinstance(job, tuple) else job}: {error}")
            return None

    with ThreadPoolExecutor(4) as pool:
        out = [record for record in pool.map(one, jobs) if record]
    return out


# --- KayKit (Kay Lousberg) ---

KAYKIT_REPOS = [f"KayKit-Game-Assets/KayKit-{name}-1.0" for name in (
    "Character-Pack-Adventures", "Character-Pack-Skeletons", "City-Builder-Bits", "Dungeon-Remastered",
    "Furniture-Bits", "Halloween-Bits", "Medieval-Hexagon-Pack", "Prototype-Bits", "Restaurant-Bits",
    "Space-Base-Bits")]


def _kaykit_stem(path: str) -> str:
    name = PurePosixPath(path).name
    for suffix in (".gltf.glb", ".glb", ".gltf"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return PurePosixPath(path).stem


def kaykit(http: Http, log: Callable[[str], None]) -> list[dict]:
    records: list[dict] = []
    for repo in KAYKIT_REPOS:
        commit = _commit(http, repo)
        files = _tree(http, repo, commit)
        known, by_name = set(files), _by_name(files)
        pack = repo.split("/")[1].lower().removeprefix("kaykit-").removesuffix("-1.0")
        pack_title = "KayKit " + pack.replace("-", " ").title()
        credit = f"{pack_title} by Kay Lousberg (kaylousberg.com), CC0"
        models = [p for p in files if p.lower().endswith((".gltf", ".glb"))
                  and "gltf" in PurePosixPath(p).parts[:-1] and "Samples" not in PurePosixPath(p).parts]
        stems: dict[str, str] = {}
        for model in models:
            stem = _kaykit_stem(model)
            if stem in stems:  # the same name in two folders: tell them apart by folder
                stem = f"{PurePosixPath(model).parent.name}_{stem}"
            stems[model] = stem

        def work(model: str, repo=repo, commit=commit, known=known, by_name=by_name, pack=pack,
                 pack_title=pack_title, credit=credit, stems=stems):
            doc = gltf_json(http, raw_url(repo, commit, model))
            deps = dependencies(doc, model, known, by_name)
            if deps is None:
                raise ValueError("a file it references is not in the repository")
            parts = PurePosixPath(model).parts
            folders = parts[parts.index("gltf") + 1:-1]
            tags = [*_tag_words(stems[model]), *(w for f in folders for w in _words(f))]
            if "Characters" in parts:
                tags.append("character")
            return _record("kaykit", "Kay Lousberg", credit, pack, pack_title, stems[model],
                           f"https://github.com/{repo}", repo, commit, model, deps, doc, tags, None)

        found = _measure_all(models, work, log, pack_title)
        log(f"sozai: {pack_title}: {len(found)} models")
        records.extend(found)
    return records


# --- Polygonal Mind (CC0 metaverse packs, converted to GLB by ToxSam) ---

PM_REGISTRY = "ToxSam/open-source-3D-assets"
PM_MODELS = "ToxSam/cc0-models-Polygonal-Mind"


def _repo_path(url: str, repo: str) -> str | None:
    """projects/x/y.glb from https://raw.githubusercontent.com/<repo>/<branch>/projects/x/y.glb."""
    prefix = f"{RAW}/{repo}/"
    if not url or not url.startswith(prefix):
        return None
    return unquote(url[len(prefix):].split("/", 1)[1]) if "/" in url[len(prefix):] else None


def polygonalmind(http: Http, log: Callable[[str], None]) -> list[dict]:
    registry = _commit(http, PM_REGISTRY)
    commit = _commit(http, PM_MODELS)
    files = _tree(http, PM_MODELS, commit)
    known, by_name = set(files), _by_name(files)
    projects = json.loads(http.get(raw_url(PM_REGISTRY, registry, "data/projects.json")))
    jobs = []
    for project in projects:
        if not project.get("is_public", True) or str(project.get("license", "CC0")).upper() != "CC0":
            continue
        pack = project["id"].removeprefix("pm-")
        pack_title = _title(project.get("name") or pack)
        assets = json.loads(http.get(raw_url(PM_REGISTRY, registry, "data/" + project["asset_data_file"])))
        for asset in assets:
            model = _repo_path(asset.get("model_file_url"), PM_MODELS)
            if asset.get("is_draft") or not asset.get("is_public", True) or model not in known:
                continue
            jobs.append((project, pack, pack_title, asset, model))
    seen: set[str] = set()

    def work(job):
        project, pack, pack_title, asset, model = job
        doc = gltf_json(http, raw_url(PM_MODELS, commit, model))
        deps = dependencies(doc, model, known, by_name)
        if deps is None:
            raise ValueError("a file it references is not in the repository")
        traits = {str(t.get("trait_type")).lower(): t.get("value")
                  for t in (asset.get("metadata") or {}).get("attributes") or [] if t.get("value")}
        stem = PurePosixPath(model).stem
        thumbnail = _repo_path(asset.get("thumbnail_url"), PM_MODELS)
        tags = [*_tag_words(asset.get("name") or stem), *(w for v in traits.values() for w in _words(str(v)))]
        return _record("polygonalmind", "Polygonal Mind",
                       f"{pack_title} by Polygonal Mind, CC0 (GLB conversion: github.com/{PM_MODELS})",
                       pack, pack_title, stem, project.get("github_url") or f"https://github.com/{PM_MODELS}",
                       PM_MODELS, commit, model, deps, doc, tags,
                       raw_url(PM_MODELS, commit, thumbnail) if thumbnail in known else None,
                       {"traits": traits} if traits else None)

    records = []
    for record in _measure_all(jobs, work, log, "Polygonal Mind"):
        if record["id"] not in seen:  # the registry lists a few files twice
            seen.add(record["id"])
            records.append(record)
    log(f"sozai: Polygonal Mind: {len(records)} models from {len(projects)} packs")
    return records


LIBRARIES: dict[str, Callable[[Http, Callable[[str], None]], list[dict]]] = {
    "kaykit": kaykit, "polygonalmind": polygonalmind}


# --- shared steps ---

def group_variants(records: list[dict]) -> None:
    """Models of one pack that differ only by a colour, a number or a letter (Bush01..Bush09,
    chair_A / chair_B, building_blue / building_red) fold into one search result with variants."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        pack, _, stem = record["source_id"].partition("/")
        words = [w.lower() for w in name_words(stem)]
        words = [w for w in words if w not in COLOURS and not w.isdigit() and len(w) > 1]
        if words:
            groups.setdefault(f"{record['source']}:{pack}/{' '.join(words)}", []).append(record)
    for key, members in groups.items():
        if len(members) > 1:
            for record in members:
                record["variant_group"] = key


def materialise(downloads: dict, root: Path) -> Path:
    """Download a record's files (cached) into root, laid out as the model expects; returns the model."""
    from sozai.fetch import _place, download, inside

    files: dict[str, str] = downloads["files"]
    base = posixpath.commonpath([posixpath.dirname(p) or "." for p in files]) if len(files) > 1 \
        else posixpath.dirname(downloads["main"])
    base = "" if base in (".", "") else base

    def local(path: str) -> str:
        return posixpath.relpath(path, base) if base else path

    for path, url in files.items():
        _place(download(url), inside(root, local(path)))
    return inside(root, local(downloads["main"]))


def render_previews(records: list[dict], previews_dir: Path, log: Callable[[str], None]) -> dict:
    """Render a preview for every record without one (earlier renders are reused)."""
    from sozai import convert, thumbs

    todo: dict[str, list[dict]] = {}
    reused = 0
    for record in records:
        if record.get("thumbnail_url"):
            continue
        png = thumbs.rendered_path(previews_dir, record)
        if png.is_file():
            thumbs._attach(record, png)
            reused += 1
        else:
            todo.setdefault(record["source_id"].split("/", 1)[0], []).append(record)
    missing = sum(len(group) for group in todo.values())
    if not todo:
        return {"rendered": 0, "reused": reused, "failed": 0}
    if convert.find_blender() is None:
        log(f"sozai: {missing} models have no preview; install Blender to render them")
        return {"rendered": 0, "reused": reused, "failed": missing}

    def pack(group: list[dict]) -> int:
        with tempfile.TemporaryDirectory(prefix="sozai-render-") as tmp:
            jobs = []
            for i, record in enumerate(group):
                try:
                    jobs.append((record, materialise(record["downloads"], Path(tmp) / str(i))))
                except Exception as error:
                    log(f"sozai: {record['id']}: download failed: {error}")
            written = thumbs.render([(model, thumbs.rendered_path(previews_dir, r)) for r, model in jobs])
        done = 0
        for record, _ in jobs:
            png = thumbs.rendered_path(previews_dir, record)
            if png in written:
                thumbs._attach(record, png)
                done += 1
        return done

    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    log(f"sozai: rendering {missing} previews with Blender ({workers} packs at a time)")
    with ThreadPoolExecutor(workers) as pool:
        rendered = sum(pool.map(pack, todo.values()))
    return {"rendered": rendered, "reused": reused, "failed": missing - rendered}


def ingest(source: str, log: Callable[[str], None] = print, thumbnails: bool = True) -> dict:
    """Read one repository library into <data dir>/catalog/<source>.jsonl."""
    from sozai.scale import calibrate

    if source not in LIBRARIES:
        raise ValueError(f"unknown library {source!r}; known: {', '.join(LIBRARIES)}")
    records = LIBRARIES[source](Http(pause=0.05), log)
    if not records:
        raise RuntimeError(f"{source}: no models found")
    scale = calibrate(records, source)
    group_variants(records)
    out_dir = paths.data_dir() / "catalog"
    out_dir.mkdir(parents=True, exist_ok=True)
    previews = render_previews(records, out_dir / "previews", log) if thumbnails else None
    target = out_dir / f"{source}.jsonl"
    tmp = target.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    tmp.replace(target)
    return {"source": source, "packs": len({r["source_id"].split("/", 1)[0] for r in records}),
            "models": len(records), "catalog": str(target),
            "calibrated_packs": len(scale["calibrated"]), "uncalibrated_packs": scale["uncalibrated"],
            **({"previews": previews} if previews else {})}
