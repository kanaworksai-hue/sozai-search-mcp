"""Libraries published as public GitHub repositories: KayKit, Polygonal Mind and the Open Source
Avatars registry (all CC0), and NASA 3D Resources (NASA media usage guidelines).

Maintainers run `sozai ingest kaykit|polygonalmind|opensourceavatars|nasa`. Nothing is cloned: the file
list comes from the GitHub API (two calls per repository) and each model is measured from its glTF
JSON alone, read from raw.githubusercontent.com (for a .glb only the JSON chunk, with a Range
request). Every record pins the commit it was read at, so its download links keep pointing at the
same files when a repository changes later. Models that ship without a preview get one rendered
with Blender, from their files downloaded into the cache.

Open Source Avatars keeps only its registry on GitHub; the VRM files live on Arweave (permanent
storage), read the same way. Avatars kept only on public IPFS gateways are left out: those
gateways stopped serving files to programs in 2026.

NASA keeps no metadata beside its files: each folder's description and small previews come from
its page on science.nasa.gov, read once per folder at ingest time, a second apart. NASA models
share no unit, so their sizes stay in the file's units (attributes.size_units) and none is reported.

Users get the records with the published data; fetch_asset downloads a model's files from GitHub
(or Arweave) and packs them into one .glb (an avatar stays one .vrm).
"""
from __future__ import annotations

import html
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


def _ranged(url: str) -> str:
    """Where a file can be read in parts: arweave.net/<id> redirects to a host that ignores Range,
    arweave.net/raw/<id> serves the same bytes and honours it."""
    parsed = urlparse(url)
    if parsed.netloc == "arweave.net" and re.fullmatch(r"/[A-Za-z0-9_-]{43}", parsed.path):
        return f"https://arweave.net/raw{parsed.path}"
    return url


def gltf_json(http: Http, url: str, binary: bool | None = None) -> dict:
    """The glTF JSON of a .gltf, or of a .glb (or .vrm) from its first bytes only. binary says
    which it is when the URL has no file extension (Arweave)."""
    if binary is None:
        binary = urlparse(url).path.lower().endswith((".glb", ".vrm"))
    raw = {"Accept": "*/*"}  # Arweave answers "Accept: application/json" with JSON about the file
    if not binary:
        return parse_gltf(http.get(url, raw))[0]
    url = _ranged(url)
    head = http.get(url, {**raw, "Range": f"bytes=0-{HEAD_BYTES - 1}"})  # a server ignoring Range sends it all
    if head[:4] != b"glTF" or len(head) < 20:
        raise ValueError(f"{url}: not a GLB")
    length = struct.unpack("<I", head[12:16])[0]
    if len(head) < 20 + length:
        head += http.get(url, {**raw, "Range": f"bytes={len(head)}-{19 + length}"})
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


def github_downloads(repo: str, commit: str, model: str, deps: dict[str, str]) -> dict:
    return {"repo": f"https://github.com/{repo}", "commit": commit, "main": model,
            "files": {model: raw_url(repo, commit, model),
                      **{want: raw_url(repo, commit, have) for want, have in deps.items()}}}


def _record(source: str, author: str, credit: str, pack: str, pack_title: str, stem: str, page: str,
            downloads: dict, doc: dict, tags: list[str], thumbnail: str | None,
            attributes: dict | None = None, title: str | None = None, style: str = "lowpoly",
            description: str = "") -> dict | None:
    facts = measure_doc(doc)
    if not facts["triangles"]:
        return None  # no geometry: an animation-only file, an empty scene
    size = facts["size"] or [None, None, None]
    return {
        "id": f"{source}:{pack}/{stem}",
        "source": source,
        "source_id": f"{pack}/{stem}",
        "title": title or title_of(stem),
        "url": page,
        "authors": [author],
        "licence": "CC0-1.0",
        "redistributable": True,
        "credit": credit,
        "kind": "model",
        "style": style,
        "creation": "modeled",
        "category": source,  # the pack name is in attributes.collection (see catalog.ingest_zip)
        "tags": list(dict.fromkeys(tags)),
        "description": description,
        "size_x": size[0], "size_y": size[1], "size_z": size[2],
        "size_source": "measured" if facts["size"] else None,
        "polycount": facts["triangles"] or None,
        "max_texture_px": None,
        "tileable": False,
        "rigged": facts["rigged"],
        "animated": facts["animated"],
        "thumbnail_url": thumbnail,
        "acquire": "direct",
        "downloads": downloads,
        "attributes": {"collection": pack_title, "animations": facts["animations"],
                       "format": PurePosixPath(downloads["main"]).suffix.lower().lstrip("."), **(attributes or {})},
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
            if stem in stems.values():  # the same name in two folders: tell them apart by folder
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
                           f"https://github.com/{repo}", github_downloads(repo, commit, model, deps), doc, tags, None)

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
                       github_downloads(PM_MODELS, commit, model, deps), doc, tags,
                       raw_url(PM_MODELS, commit, thumbnail) if thumbnail in known else None,
                       {"traits": traits} if traits else None)

    records = []
    for record in _measure_all(jobs, work, log, "Polygonal Mind"):
        if record["id"] not in seen:  # the registry lists a few files twice
            seen.add(record["id"])
            records.append(record)
    log(f"sozai: Polygonal Mind: {len(records)} models from {len(projects)} packs")
    return records


# --- Open Source Avatars (a CC0 registry of VRM avatars by ToxSam; files on Arweave) ---

OSA_REGISTRY = "ToxSam/open-source-avatars"
OSA_PAGE = "https://www.opensourceavatars.com/en/avatars/"
# Hosts that serve files to programs and keep them: Arweave is permanent storage, GitHub pins a
# commit. Public IPFS gateways (dweb.link, ipfs.io, w3s.link) answer programs with 429 since 2026.
STABLE_HOSTS = ("arweave.net", "raw.githubusercontent.com")
CREATORS = {"Polygonal-Mind": "Polygonal Mind"}


def _stable(url: str | None) -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(host == h or host.endswith("." + h) for h in STABLE_HOSTS)


def _file_name(url: str, stem: str, fmt: str) -> str:
    """The model's file name: from its URL when it has one, else <stem>.<format> (Arweave URLs
    are bare ids)."""
    name = PurePosixPath(unquote(urlparse(url).path)).name
    return name if name.lower().endswith((".vrm", ".glb")) else f"{stem}.{fmt}"


def _spaced(name: str) -> str:
    """CoolShovel -> Cool Shovel, COOLFries -> COOL Fries; names already spaced stay as they are."""
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", re.sub(r"([a-z])([A-Z])", r"\1 \2", name.strip()))


def _avatar_stem(avatar: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", avatar.get("name") or "").strip("_") or avatar["id"][:8]
    number = str((avatar.get("metadata") or {}).get("number") or "")
    return f"{number}_{name}" if number.isdigit() else name


def opensourceavatars(http: Http, log: Callable[[str], None]) -> list[dict]:
    registry = _commit(http, OSA_REGISTRY)
    projects = json.loads(http.get(raw_url(OSA_REGISTRY, registry, "data/projects.json")))
    jobs = []
    skipped: dict[str, int] = {}
    for project in projects:
        if not project.get("is_public", True) or str(project.get("license")).upper() != "CC0":
            log(f"sozai: Open Source Avatars: left out {project.get('name')} (licence {project.get('license')})")
            continue
        pack = project["id"].lower().removesuffix("-collection")
        pack_title = project.get("name") or pack
        creator = CREATORS.get(project.get("creator_id"), project.get("creator_id") or "unknown")
        avatars = json.loads(http.get(raw_url(OSA_REGISTRY, registry, "data/" + project["avatar_data_file"])))
        stems: set[str] = set()
        for avatar in avatars:
            if avatar.get("is_draft") or not avatar.get("is_public", True):
                continue
            meta = avatar.get("metadata") or {}
            alternates = meta.get("alternateModels") or {}
            fmt = str(avatar.get("format") or "vrm").lower()
            url = avatar.get("model_file_url")
            if not _stable(url):  # the same avatar as a GLB on Arweave, when the VRM is only on IPFS
                fmt, url = next(((k, v) for k, v in alternates.items() if k in ("vrm", "glb") and _stable(v)),
                                (fmt, None))
            if not url:
                skipped[pack_title] = skipped.get(pack_title, 0) + 1
                continue
            stem = _avatar_stem(avatar)
            while stem in stems:
                stem += "_b"
            stems.add(stem)
            extra = {k: v for k, v in alternates.items() if not k.startswith("voxel_") and _stable(v) and v != url}
            jobs.append((project, pack, pack_title, creator, avatar, stem, url, fmt, False, extra, f"{pack}/{stem}"))
            if _stable(alternates.get("voxel_vrm")):
                voxel = {k.removeprefix("voxel_"): v for k, v in alternates.items()
                         if k.startswith("voxel_") and k != "voxel_vrm" and _stable(v)}
                jobs.append((project, pack, pack_title, creator, avatar, f"{stem}_Voxel",
                             alternates["voxel_vrm"], "vrm", True, voxel, f"{pack}/{stem}_Voxel"))
    for title, count in skipped.items():
        log(f"sozai: Open Source Avatars: left out {count} {title} avatars (files only on IPFS gateways)")

    def work(job):
        _, pack, pack_title, creator, avatar, stem, url, fmt, voxel, extra, _ = job
        doc = gltf_json(http, url, binary=True)
        meta = avatar.get("metadata") or {}
        traits = {str(t["trait_type"]).lower(): t["value"] for t in meta.get("attributes") or []
                  if isinstance(t, dict) and t.get("trait_type") and t.get("value") and not t.get("display_type")}
        name = _spaced(avatar.get("name") or stem)
        main = _file_name(url, stem, fmt)
        tags = [*_tag_words(name), "avatar", "character", "humanoid", "vrm" if fmt == "vrm" else "glb",
                *(w for v in traits.values() for w in _words(str(v))), *(["voxel"] if voxel else [])]
        attributes = {"avatar_id": avatar["id"], **({"traits": traits} if traits else {}),
                      **({"alternates": extra} if extra else {}), **({"number": meta["number"]} if meta.get("number") else {})}
        record = _record(
            "opensourceavatars", creator, f"{pack_title} by {creator}, CC0 (via Open Source Avatars, opensourceavatars.com)",
            pack, pack_title, stem, OSA_PAGE + avatar["id"],
            {"registry": f"https://github.com/{OSA_REGISTRY}", "commit": registry, "main": main, "files": {main: url}},
            doc, tags, avatar.get("thumbnail_url") if _stable(avatar.get("thumbnail_url")) and not voxel else None,
            attributes, title=f"{name} (voxel)" if voxel else name,
            style="lowpoly" if creator == "Polygonal Mind" else "stylized",
            description=(avatar.get("description") or "").strip())
        if record and _stable((meta.get("alternateModels") or {}).get("voxel_vrm")):  # the avatar and its voxel version
            record["variant_group"] = f"opensourceavatars:{pack}/{stem.removesuffix('_Voxel')}"
        return record

    records = _measure_all(jobs, work, log, "Open Source Avatars")
    log(f"sozai: Open Source Avatars: {len(records)} avatars")
    return records


# --- NASA 3D Resources (spacecraft, rovers, telescopes, asteroids; NASA media usage guidelines) ---

NASA_REPO = "nasa/NASA-3D-Resources"
NASA_SITE = "https://science.nasa.gov/3d-resources/"
NASA_ASSETS = "https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/"
NASA_THUMB = "/jcr:content/renditions/cq5dam.thumbnail.319.319.png"  # ~20 KB; the repository's PNGs are ~1 MB
NASA_LICENCE = "LicenseRef-NASA-Media-Guidelines"
NASA_GUIDELINES = "https://www.nasa.gov/nasa-brand-center/images-and-media/"
NASA_CREDIT = (f"NASA 3D Resources (github.com/{NASA_REPO}). Courtesy NASA; not subject to copyright in the US. "
               f"Do not imply NASA endorsement; NASA logos and insignia need permission: {NASA_GUIDELINES}")
NASA_SCALE = ("NASA models use no common unit (some are in metres, others in arbitrary units); "
              "size_units are the file's own units: scale it against the real object")
SITE_PAUSE = 1.0  # science.nasa.gov asks crawlers for one second between requests


def _nasa_slug(folder: str) -> str:
    """The science.nasa.gov page of a repository folder: Aqua (A) -> aqua-a."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", folder.lower())).strip("-")


def _meta(page: str, prop: str) -> str:
    match = re.search(rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]*)"', page)
    return html.unescape(match.group(1)).strip() if match else ""


def nasa_page(http: Http, folder: str) -> dict:
    """What the folder's page on science.nasa.gov says: a one-paragraph description and the
    previews it shows ({png file name: small preview URL}). Empty when there is no page."""
    url = NASA_SITE + _nasa_slug(folder) + "/"
    try:
        page = http.get(url, {"Accept": "text/html"}).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    previews = {}
    for found in re.findall(r'https://assets\.science\.nasa\.gov/content/dam/science/cds/3d/resources/'
                            r'[^"\s]+?\.png(?=/jcr:content|["\s])', page):
        previews.setdefault(unquote(found.rsplit("/", 1)[1]).lower(), found + NASA_THUMB)
    description = _meta(page, "og:description")
    return {"page": url, "description": description.rstrip(" .") + "." if description else "", "previews": previews}


def nasa(http: Http, log: Callable[[str], None]) -> list[dict]:
    import time

    commit = _commit(http, NASA_REPO)
    files = _tree(http, NASA_REPO, commit)
    known, by_name = set(files), _by_name(files)
    folders: dict[str, list[str]] = {}
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts) == 3 and parts[0] == "3D Models" and path.lower().endswith(".glb"):
            folders.setdefault(parts[1], []).append(path)
    skipped = sorted({PurePosixPath(p).parts[1] for p in files if p.startswith("3D Models/")
                      and len(PurePosixPath(p).parts) == 3} - set(folders))
    if skipped:
        log(f"sozai: NASA: left out {len(skipped)} folders with no .glb (.blend, .3ds or split .7z only): "
            + ", ".join(skipped))
    pages = {}
    for folder in sorted(folders):  # one page per folder, politely: these are not an API
        pages[folder] = nasa_page(http, folder)
        time.sleep(SITE_PAUSE)
    log(f"sozai: NASA: {sum(1 for p in pages.values() if p)} of {len(folders)} folders have a page on science.nasa.gov")
    jobs = [(folder, model, folder) for folder, models in folders.items() for model in models]

    def work(job):
        folder, model, _ = job
        doc = gltf_json(http, raw_url(NASA_REPO, commit, model))
        deps = dependencies(doc, model, known, by_name)
        if deps is None:
            raise ValueError("a file it references is not in the repository")
        name = PurePosixPath(model).stem
        single = len(folders[folder]) == 1
        title = folder if single else name  # folder names are written more carefully than file names
        info = pages[folder]
        previews = info.get("previews") or {}
        thumbnail = previews.get(f"{name}.png".lower()) or (next(iter(previews.values()), None) if single else None)
        if thumbnail is None and f"{posixpath.dirname(model)}/{name}.png" in known:
            thumbnail = raw_url(NASA_REPO, commit, f"{posixpath.dirname(model)}/{name}.png")
        stem = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        tags = [*_tag_words(title), *_tag_words(folder), "nasa"]
        record = _record("nasa", "NASA", NASA_CREDIT, "models", "NASA 3D Resources", stem,
                         info.get("page") or f"https://github.com/{NASA_REPO}/tree/{commit}/{quote(posixpath.dirname(model))}",
                         github_downloads(NASA_REPO, commit, model, deps), doc, tags, thumbnail,
                         {"folder": folder}, title=title, style="photoreal", description=info.get("description", ""))
        if record:
            record.update(licence=NASA_LICENCE, redistributable=True)
            sizes = [record["size_x"], record["size_y"], record["size_z"]]
            record["attributes"]["size_units"] = sizes if any(sizes) else None
            record["attributes"]["scale"] = {"factor": None, "reference": NASA_SCALE}
            record["size_x"] = record["size_y"] = record["size_z"] = record["size_source"] = None
        return record

    records = _measure_all(jobs, work, log, "NASA")
    _nasa_variants(records)
    log(f"sozai: NASA: {len(records)} models from {len(folders)} folders")
    return records


def _nasa_variants(records: list[dict]) -> None:
    """NASA marks other versions of one object with a letter: Aqua (A), Aqua (B). Only those fold
    together; numbers name different objects (Landsat 7 and 8, ICESat and ICESat-2)."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        base = re.sub(r"\s*\([A-Z]\)", "", record["title"]).strip()
        groups.setdefault(base, []).append(record)
    for base, members in groups.items():
        for record in members:
            record["variant_group"] = f"nasa:models/{base}" if len(members) > 1 else None


LIBRARIES: dict[str, Callable[[Http, Callable[[str], None]], list[dict]]] = {
    "kaykit": kaykit, "polygonalmind": polygonalmind, "opensourceavatars": opensourceavatars, "nasa": nasa}


# --- shared steps ---

def unique_ids(records: list[dict], log: Callable[[str], None]) -> None:
    """Give a numbered id to a record whose id another record already has (two folders holding the
    same file name, names that differ only in punctuation): the index allows each id once."""
    seen: set[str] = {r["id"] for r in records}
    taken: set[str] = set()
    for record in records:
        if record["id"] in taken:
            number = 2
            while f"{record['id']}_{number}" in seen:
                number += 1
            old = record["id"]
            record["id"] += f"_{number}"
            record["source_id"] += f"_{number}"
            seen.add(record["id"])
            log(f"sozai: {old} appears twice; the second is {record['id']}")
        taken.add(record["id"])


SELF_GROUPED = {"nasa"}  # libraries whose own rule decides variants (see _nasa_variants)
SIZE_SPREAD = 3.0  # variants of one model are about the same size; a 42 m tower is not a 0.4 m lamp


def _largest(record: dict) -> float | None:
    sizes = [record.get(k) for k in ("size_x", "size_y", "size_z")]
    if not any(sizes):
        sizes = (record.get("attributes") or {}).get("size_units") or []
    return max((s for s in sizes if s), default=None)


def _same_size(members: list[dict]) -> list[dict]:
    """The members within SIZE_SPREAD of the group's median size (all of them when sizes are unknown)."""
    known = sorted(s for s in map(_largest, members) if s)
    if not known:
        return members
    median = known[len(known) // 2]
    return [r for r in members if (s := _largest(r)) is None or median / SIZE_SPREAD <= s <= median * SIZE_SPREAD]


def group_variants(records: list[dict]) -> None:
    """Models of one pack that differ only by a colour, a number or a letter (Bush01..Bush09,
    chair_A / chair_B, building_blue / building_red) fold into one search result with variants,
    unless their sizes say they are different things (Tower Station 01 and Tower Station Light)."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        if record.get("variant_group") or record["source"] in SELF_GROUPED:
            continue  # grouped by its library already (an avatar and its voxel version, NASA)
        pack, _, stem = record["source_id"].partition("/")
        words = [w.lower() for w in name_words(stem)]
        words = [w for w in words if w not in COLOURS and not w.isdigit() and len(w) > 1]
        if words:
            groups.setdefault(f"{record['source']}:{pack}/{' '.join(words)}", []).append(record)
    for key, members in groups.items():
        kept = _same_size(members) if len(members) > 1 else []
        for record in kept if len(kept) > 1 else []:
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
    unique_ids(records, log)
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
