"""Render a preview image for kit models that ship without one (most of Quaternius).

Offline, at ingest time, through headless Blender: one Blender process per kit renders all of that
kit's models with the Workbench engine (studio light, textures or material colours, a three-quarter
view from the front), so a pack of 300 models costs one start-up. Renders are kept on disk and
reused on the next ingest; the MCP server only ever reads the finished PNGs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sozai.catalog import extract_model
from sozai import convert

SIZE = 320

SCRIPT = r'''
import bpy, json, sys
from mathutils import Vector

jobs, size = json.load(open(sys.argv[sys.argv.index("--") + 1])), int(sys.argv[-1])
# glTF models face +Z, which Blender's import turns into -Y: look from the front, right and above
DIRECTION = Vector((0.75, -1.3, 0.85)).normalized()


def setup(scene):
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light, shading.color_type = "STUDIO", "TEXTURE"
    shading.show_cavity = True
    shading.cavity_type, shading.curvature_ridge_factor, shading.curvature_valley_factor = "WORLD", 0.6, 0.8
    scene.display.render_aa = "8"
    scene.world = bpy.data.worlds.new("sozai")
    scene.world.color = (0.62, 0.64, 0.67)
    scene.view_settings.view_transform = "Standard"
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    # WebP keeps a preview near 10 KB (a PNG is about 75): thousands of them stay small enough to publish
    scene.render.image_settings.file_format, scene.render.image_settings.color_mode = "WEBP", "RGB"
    scene.render.image_settings.quality = 85


def material_colours():
    # Workbench shows a material's viewport colour, which importers leave white: copy the base colour.
    # A texture that failed to load would render magenta: drop it and show the colour instead.
    for mat in bpy.data.materials:
        if not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image and not node.image.packed_file and node.image.size[0] == 0:
                node.image = None
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED" and not node.inputs["Base Color"].is_linked:
                mat.diffuse_color = node.inputs["Base Color"].default_value
                break


def vertex_coloured(meshes):
    # Models painted with vertex colours and no textures (common in low-poly packs)
    has_image = any(n.type == "TEX_IMAGE" and n.image for m in bpy.data.materials if m.use_nodes for n in m.node_tree.nodes)
    return not has_image and meshes and all(o.data.color_attributes for o in meshes)


for src, dst in jobs:
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        ext = src.lower().rsplit(".", 1)[-1]
        if ext in ("glb", "gltf"):
            bpy.ops.import_scene.gltf(filepath=src)
        elif ext == "obj":
            bpy.ops.wm.obj_import(filepath=src)
        elif ext == "fbx":
            bpy.ops.import_scene.fbx(filepath=src)
        scene = bpy.context.scene
        setup(scene)
        material_colours()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        meshes = [o for o in scene.objects if o.type == "MESH" and o.visible_get() and not o.hide_render]
        if vertex_coloured(meshes):
            scene.display.shading.color_type = "VERTEX"
        points = [o.evaluated_get(depsgraph).matrix_world @ Vector(c)
                  for o in meshes for c in o.evaluated_get(depsgraph).bound_box]
        if not points:
            print("SOZAI_FAIL", src, "no visible mesh", flush=True)
            continue
        lo = Vector([min(p[i] for p in points) for i in range(3)])
        hi = Vector([max(p[i] for p in points) for i in range(3)])
        centre, radius = (lo + hi) / 2, max((hi - lo).length / 2, 1e-4)
        data = bpy.data.cameras.new("sozai")
        data.type = "ORTHO"
        camera = bpy.data.objects.new("sozai", data)
        scene.collection.objects.link(camera)
        camera.location = centre + DIRECTION * radius * 4
        camera.rotation_euler = (-DIRECTION).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        local = [camera.matrix_world.inverted() @ p for p in points]
        xs, ys = [v.x for v in local], [v.y for v in local]
        data.ortho_scale = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.1
        data.shift_x = (max(xs) + min(xs)) / 2 / data.ortho_scale
        data.shift_y = (max(ys) + min(ys)) / 2 / data.ortho_scale
        data.clip_start, data.clip_end = radius * 0.01, radius * 10
        scene.camera = camera
        scene.render.filepath = dst
        bpy.ops.render.render(write_still=True)
        print("SOZAI_OK", dst, flush=True)
    except Exception as error:
        print("SOZAI_FAIL", src, repr(error), flush=True)
'''


def render(jobs: list[tuple[Path, Path]], size: int = SIZE, timeout: float = 3600) -> set[Path]:
    """Render (model, image) pairs in one Blender run. Returns the images that were written."""
    blender = convert.find_blender()
    if not blender or not jobs:
        return set()
    with tempfile.TemporaryDirectory(prefix="sozai-render-") as tmp:
        script, job_file = Path(tmp) / "render.py", Path(tmp) / "jobs.json"
        script.write_text(SCRIPT)
        job_file.write_text(json.dumps([[str(s), str(d)] for s, d in jobs]))
        for _, dst in jobs:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([blender, "-b", "--factory-startup", "-noaudio", "--python", str(script), "--",
                            str(job_file), str(size)], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            pass  # keep whatever was rendered before the timeout
    return {Path(d) for _, d in jobs if Path(d).is_file()}


def rendered_path(previews_dir: Path, record: dict) -> Path:
    slug, _, stem = record["source_id"].partition("/")
    return previews_dir / record["source"] / slug / "rendered" / (re.sub(r"[^\w.-]+", "_", stem) + ".webp")


def _render_kit(kit_zip: str, records: list[dict], previews_dir: Path) -> int:
    jobs: list[tuple[Path, Path]] = []
    done = 0
    with tempfile.TemporaryDirectory(prefix="sozai-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(kit_zip) as zf:
            for record in records:
                downloads = record["downloads"]
                converted = downloads.get("converted")
                if converted and Path(converted).is_file():
                    source = Path(converted)
                else:
                    source = extract_model(zf, downloads, root / str(len(jobs)))
                jobs.append((source, rendered_path(previews_dir, record)))
        written = render(jobs)
    for record, (_, png) in zip(records, jobs):
        if png in written:
            _attach(record, png)
            done += 1
    return done


def _attach(record: dict, png: Path) -> None:
    record["thumbnail_url"] = png.resolve().as_uri()
    record.setdefault("attributes", {})["thumbnail"] = "rendered"


def render_missing(records: list[dict], previews_dir: Path, log: Callable[[str], None] = print,
                   workers: int | None = None) -> dict:
    """Give every record without a thumbnail a rendered one. Earlier renders are reused."""
    todo: dict[str, list[dict]] = {}
    reused = 0
    for record in records:
        if record.get("thumbnail_url") and (record.get("attributes") or {}).get("thumbnail") != "rendered":
            continue  # the kit's own preview
        png = rendered_path(previews_dir, record)
        if png.is_file():
            _attach(record, png)
            reused += 1
        else:
            record["thumbnail_url"] = None
            kit_zip = (record.get("downloads") or {}).get("kit_zip")
            if kit_zip and Path(kit_zip).is_file():
                todo.setdefault(kit_zip, []).append(record)
    missing = sum(len(group) for group in todo.values())
    if not todo:
        return {"rendered": 0, "reused": reused, "failed": 0}
    if convert.find_blender() is None:
        log(f"sozai: {missing} models have no preview; install Blender to render them")
        return {"rendered": 0, "reused": reused, "failed": missing}
    workers = workers or max(1, min(4, (os.cpu_count() or 2) // 2))
    log(f"sozai: rendering {missing} previews from {len(todo)} kits with Blender ({workers} at a time)")
    rendered = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_render_kit, kit, group, previews_dir): (kit, group) for kit, group in todo.items()}
        for future in futures:
            kit, group = futures[future]
            try:
                count = future.result()
            except Exception as error:  # one broken kit must not cost the other renders
                log(f"sozai: {Path(kit).name}: previews failed: {error}")
                continue
            rendered += count
            log(f"sozai: {Path(kit).name}: {count}/{len(group)} previews rendered")
    return {"rendered": rendered, "reused": reused, "failed": missing - rendered}
