"""Offline: compute every asset's vectors with the full EmbeddingGemma 2 on MLX (`sozai embed`).

The maintainer runs this on a Mac after `sozai build` (`pip install 'sozai-search-mcp[embed]'`). It reads the
local index, embeds each asset's text (see semantic.doc_text) and its preview image when it has
one (Poly Haven and ambientCG thumbnails from their CDNs, kit previews from disk; Megascans have
none), and writes vectors.npz. Vectors whose input did not change are reused, so a rerun after a
library update only embeds what is new.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sozai import index, paths, semantic

IMAGE_TOKENS = 140
BATCH = 8


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:16]


class DocEmbedder:
    def __init__(self):
        import mlx.core as mx
        import numpy as np
        from mlx_vlm import load

        self.mx, self.np = mx, np
        mx.set_cache_limit(1 << 30)
        self.model, self.processor = load(semantic.DOC_MODEL)

    def _run(self, inputs) -> "np.ndarray":
        mx, np = self.mx, self.np
        feed = {k: mx.array(np.asarray(v)) for k, v in inputs.items() if not k.startswith("num_soft_tokens")}
        out = self.model(**feed).text_embeds
        mx.eval(out)
        return np.array(out.astype(mx.float32))

    def texts(self, texts: list[str]):
        encoded = self.processor.tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="np")
        return self._run(dict(encoded))

    def images(self, images: list):
        inputs = self.processor(text=["<|image|>"] * len(images), images=[[im] for im in images], return_tensors="np",
                                padding=True, images_kwargs={"max_soft_tokens": IMAGE_TOKENS})
        return self._run(inputs)


def _quantise(rows):
    import numpy as np

    rows = np.asarray(rows, np.float32)[:, :semantic.DIM]
    rows /= np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-12)
    return np.clip(np.round(rows * 127), -127, 127).astype(np.int8)


def _image_bytes(url: str) -> bytes | None:
    from sozai.fetch import download
    from sozai.view import thumbnail_bytes

    try:
        if url.startswith("https://"):  # some libraries' previews are large PNGs (up to ~20 MB)
            return download(url, max_bytes=64 << 20).read_bytes()
        return thumbnail_bytes(url)  # file:// from the catalog folder
    except Exception:
        return None


def build_vectors(path: Path | None = None, log: Callable[[str], None] = print, images: bool = True) -> dict:
    import numpy as np
    from PIL import Image

    path = path or semantic.vectors_path()
    conn = index.connect(paths.index_path())
    try:
        records = [index.decode(row) for row in conn.execute("SELECT * FROM assets ORDER BY id")]
    finally:
        conn.close()
    old = {}
    if path.is_file():
        data = np.load(path, allow_pickle=False)
        for kind in ("text", "image"):
            old[kind] = dict(zip((str(i) for i in data[f"{kind}_ids"]),
                                 zip((str(h) for h in data[f"{kind}_hashes"]), data[f"{kind}_vectors"])))
    embedder = None

    def need_embedder():
        nonlocal embedder
        if embedder is None:
            log("sozai: loading EmbeddingGemma 2 (MLX)")
            embedder = DocEmbedder()
        return embedder

    out: dict[str, tuple[list, list, list]] = {}
    # text: one vector per asset
    ids, hashes, vectors, todo = [], [], [], []
    for record in records:
        text = semantic.doc_text(record)
        digest = _hash(text)
        ids.append(record["id"])
        hashes.append(digest)
        known = old.get("text", {}).get(record["id"])
        vectors.append(known[1] if known and known[0] == digest else None)
        if vectors[-1] is None:
            todo.append((len(vectors) - 1, text))
    log(f"sozai: text vectors: {len(records) - len(todo)} reused, {len(todo)} to embed")
    for start in range(0, len(todo), 32):
        chunk = todo[start:start + 32]
        rows = _quantise(need_embedder().texts([t for _, t in chunk]))
        for (i, _), row in zip(chunk, rows):
            vectors[i] = row
        if start % 3200 == 0:
            log(f"sozai: text {start + len(chunk)}/{len(todo)}")
    out["text"] = (ids, hashes, vectors)

    # images: one vector per asset with a preview
    ids, hashes, vectors, todo = [], [], [], []
    for record in records if images else []:
        url = record.get("thumbnail_url")
        if not url:
            continue
        digest = _hash(url)
        ids.append(record["id"])
        hashes.append(digest)
        known = old.get("image", {}).get(record["id"])
        vectors.append(known[1] if known and known[0] == digest else None)
        if vectors[-1] is None:
            todo.append((len(vectors) - 1, url))
    log(f"sozai: image vectors: {len(ids) - len(todo)} reused, {len(todo)} to embed")
    failed = set()
    with ThreadPoolExecutor(4) as pool:  # download (once, cached) while the GPU embeds
        for start in range(0, len(todo), BATCH * 8):
            chunk = todo[start:start + BATCH * 8]
            loaded = []
            for (i, url), data in zip(chunk, pool.map(lambda job: _image_bytes(job[1]), chunk)):
                try:
                    loaded.append((i, Image.open(io.BytesIO(data)).convert("RGB")))
                except Exception:
                    failed.add(i)
            for b in range(0, len(loaded), BATCH):
                batch = loaded[b:b + BATCH]
                rows = _quantise(need_embedder().images([im for _, im in batch]))
                for (i, _), row in zip(batch, rows):
                    vectors[i] = row
            if start % (BATCH * 8 * 25) == 0:
                log(f"sozai: images {start + len(chunk)}/{len(todo)}")
    keep = [i for i in range(len(ids)) if i not in failed and vectors[i] is not None]
    out["image"] = ([ids[i] for i in keep], [hashes[i] for i in keep], [vectors[i] for i in keep])

    arrays = {"model": np.array(semantic.MODEL_ID), "dim": np.array(semantic.DIM)}
    for kind, (kind_ids, kind_hashes, kind_vectors) in out.items():
        arrays[f"{kind}_ids"] = np.array(kind_ids, dtype=str)
        arrays[f"{kind}_hashes"] = np.array(kind_hashes, dtype=str)
        arrays[f"{kind}_vectors"] = (np.stack(kind_vectors) if kind_vectors
                                     else np.zeros((0, semantic.DIM), np.int8))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(path)
    report = {"path": str(path), "text": len(out["text"][0]), "image": len(out["image"][0]),
              "image_failed": len(failed), "bytes": path.stat().st_size}
    log(f"sozai: {json.dumps(report)}")
    return report


if __name__ == "__main__":
    build_vectors(log=lambda m: print(m, file=sys.stderr))
