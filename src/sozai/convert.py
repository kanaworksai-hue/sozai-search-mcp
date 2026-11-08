"""FBX -> GLB through Blender (optional; only the offline ingest uses it).

One Blender process converts a whole batch, so a kit with hundreds of FBX files costs one start-up.
Blender is found through SOZAI_BLENDER / BLENDER_PATH, the PATH, or the usual install folders.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = r'''
import bpy, json, sys
jobs = json.load(open(sys.argv[sys.argv.index("--") + 1]))
for src, dst in jobs:
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.fbx(filepath=src)
        # Many FBX exporters write a transparency factor that Blender reads as alpha 0: the glTF then
        # gets alphaMode MASK and the model is invisible in every engine. A fully transparent,
        # untextured material is never meant, so make it opaque.
        for mat in bpy.data.materials:
            for node in (mat.node_tree.nodes if mat.use_nodes else []):
                alpha = node.inputs.get("Alpha") if node.type == "BSDF_PRINCIPLED" else None
                if alpha is not None and not alpha.is_linked and alpha.default_value == 0:
                    alpha.default_value = 1.0
                    try:
                        mat.blend_method = "OPAQUE"
                    except (AttributeError, TypeError):
                        pass
        bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", export_animations=True)
        print("SOZAI_OK", dst, flush=True)
    except Exception as error:
        print("SOZAI_FAIL", src, repr(error), flush=True)
'''


def script_version() -> str:
    """Changes whenever the conversion script does, so earlier conversions are redone."""
    return hashlib.sha1(SCRIPT.encode()).hexdigest()[:12]


def find_blender() -> str | None:
    for env in ("SOZAI_BLENDER", "BLENDER_PATH"):
        if os.environ.get(env) and Path(os.environ[env]).exists():
            return os.environ[env]
    found = shutil.which("blender")
    if found:
        return found
    candidates = ["/Applications/Blender.app/Contents/MacOS/Blender"]
    if sys.platform == "win32":
        candidates += sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"), reverse=True)
    return next((c for c in candidates if Path(c).exists()), None)


def fbx_to_glb(jobs: list[tuple[Path, Path]], timeout: float = 1800) -> dict[Path, Path]:
    """Convert (fbx, glb) pairs. Returns {fbx: glb} for the ones that worked."""
    blender = find_blender()
    if not blender or not jobs:
        return {}
    with tempfile.TemporaryDirectory(prefix="sozai-blender-") as tmp:
        script, job_file = Path(tmp) / "convert.py", Path(tmp) / "jobs.json"
        script.write_text(SCRIPT)
        job_file.write_text(json.dumps([[str(s), str(d)] for s, d in jobs]))
        for _, dst in jobs:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([blender, "-b", "--factory-startup", "-noaudio", "--python", str(script), "--", str(job_file)],
                       capture_output=True, text=True, timeout=timeout)
    return {Path(s): Path(d) for s, d in jobs if Path(d).is_file()}
