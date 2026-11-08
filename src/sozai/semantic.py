"""Semantic search with EmbeddingGemma 2: queries in any language matched by meaning, against each
asset's words and its preview image (one shared vector space for text and images).

Two halves:
- Offline (the maintainer, `sozai embed`, see embed_build.py): every asset gets a text vector and,
  when it has a preview, an image vector, computed with the full model on MLX. They ship as
  vectors.npz: 256 dimensions (Matryoshka truncation), int8.
- At query time only the text model runs, as ONNX int8 on the CPU (about 45 ms a query; the same
  vectors as the full model to 4 decimals). It lives in a worker process that the MCP server starts
  on the first search and that exits by itself after IDLE_SECONDS without a query, so an idle
  server stays at the standard library's footprint. Needs `pip install sozai-search-mcp[semantic]`; without
  it, or without the model and vectors, search stays keyword-only.

This module is imported by the server for the client side and the paths, so its top level uses
only the standard library; numpy, onnxruntime and tokenizers are imported inside the worker.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from sozai import paths

MODEL_REPO = "onnx-community/embeddinggemma-2-ONNX"
# Pinned: the shipped asset vectors only match query vectors from exactly these files.
MODEL_REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
MODEL_SHA256 = {
    "tokenizer.json": "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4",
    "onnx/model_quantized.onnx": "d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953",
    "onnx/model_quantized.onnx_data": "278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b",
}
MODEL_FILES = tuple(MODEL_SHA256)
DOC_MODEL = "mlx-community/embeddinggemma-2-bf16"  # the same weights, used offline for documents and images
MODEL_ID = "embeddinggemma-2"
DIM = 256
QUERY_PREFIX = "task: search result | query: "
IDLE_SECONDS = 300
TOP_K = 300
START_TIMEOUT = 180  # seconds for the worker to load the model
QUERY_TIMEOUT = 30   # seconds for one answer
RETRY_AFTER = 600    # seconds before trying again to start a worker that failed


def model_dir() -> Path:
    return paths.data_dir() / "models" / MODEL_ID


def vectors_path() -> Path:
    return paths.data_dir() / "vectors.npz"


def deps_installed() -> bool:
    return all(importlib.util.find_spec(name) for name in ("numpy", "onnxruntime", "tokenizers"))


def model_installed() -> bool:
    return all((model_dir() / name).is_file() for name in MODEL_FILES)


def status() -> str | None:
    """None when semantic search can run, else what is missing."""
    if not deps_installed():
        return "semantic search needs the semantic extra: `uv sync --extra semantic` (or `pip install 'sozai-search-mcp[semantic]'`)"
    if not model_installed():
        return "semantic search model not downloaded: run `sozai semantic-setup`"
    if not vectors_path().is_file():
        return "no asset vectors: they come with the published data, run `sozai build`"
    return None


def install_model(log: Callable[[str], None] = print) -> Path:
    """Download the ONNX text model (about 350 MB, once) from Hugging Face, at the pinned revision."""
    from sozai.http import open_url

    folder = model_dir()
    for name, sha256 in MODEL_SHA256.items():
        target = folder / name
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        log(f"sozai: downloading {url}")
        tmp = target.with_name(f"{target.name}.{os.getpid()}.part")
        digest = hashlib.sha256()
        try:
            with open_url(url, timeout=120) as response, tmp.open("wb") as out:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != sha256:
                raise ValueError(f"{name} does not match the pinned model (checksum mismatch)")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    return folder


def doc_text(record: dict) -> str:
    """How an asset reads to the embedding model: its title, then what it is, in a few words."""
    attributes = record.get("attributes") or {}
    parts = [record.get("kind") or "", record.get("style") or "", (record.get("category") or "").replace("/", " ")]
    parts += [attributes.get("collection") or "", attributes.get("surface") or "", attributes.get("role") or ""]
    parts += [", ".join((record.get("tags") or [])[:20])]
    text = ". ".join(p for p in (" ".join(str(x).split()) for x in parts) if p)
    return f"title: {record.get('title') or 'none'} | text: {text}"


# --- worker (separate process: numpy, onnxruntime, tokenizers) ---

class Encoder:
    def __init__(self, folder: Path, vectors: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        options = ort.SessionOptions()
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(folder / "onnx" / "model_quantized.onnx"), options,
                                            providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.enable_truncation(256)
        data = np.load(vectors, allow_pickle=False)
        if str(data["model"]) != MODEL_ID or int(data["dim"]) != DIM:
            raise ValueError(f"vectors were made with {data['model']} at {data['dim']}d")
        self.matrices = {}
        for kind in ("text", "image"):
            ids, matrix = data[f"{kind}_ids"], data[f"{kind}_vectors"].astype(np.float32) / 127.0
            self.matrices[kind] = (ids, matrix)

    def embed(self, texts: list[str]):
        np = self.np
        rows = []
        for text in texts:
            encoding = self.tokenizer.encode(QUERY_PREFIX + text)
            feed = {"input_ids": np.array([encoding.ids], np.int64),
                    "attention_mask": np.array([encoding.attention_mask], np.int64),
                    **{k: np.zeros((0, 512), np.float32) for k in ("image_features", "video_features", "audio_features")}}
            vector = self.session.run(["sentence_embedding"], feed)[0][0][:DIM]
            rows.append(vector / max(np.linalg.norm(vector), 1e-12))
        return np.array(rows, np.float32)

    def search(self, texts: list[str], k: int = TOP_K) -> dict:
        """For each modality, the best k asset ids over all the given wordings, with cosines."""
        np = self.np
        queries = self.embed(texts)
        out = {}
        for kind, (ids, matrix) in self.matrices.items():
            if not len(ids):
                out[kind] = []
                continue
            scores = (matrix @ queries.T).max(axis=1)
            top = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
            top = top[np.argsort(-scores[top])]
            out[kind] = [[str(ids[i]), round(float(scores[i]), 4)] for i in top]
        return out


def worker() -> None:
    """Answer one JSON line per query on stdin; exit on EOF or after IDLE_SECONDS of silence."""
    encoder = Encoder(model_dir(), vectors_path())
    print(json.dumps({"ready": True}), flush=True)
    idle = float(os.environ.get("SOZAI_SEMANTIC_IDLE", IDLE_SECONDS))
    lines: queue.Queue[str] = queue.Queue()  # a reader thread, since select() cannot wait on pipes on Windows

    def read() -> None:
        for line in iter(sys.stdin.readline, ""):
            lines.put(line)
        lines.put("")  # EOF: the server went away

    threading.Thread(target=read, daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=idle)
        except queue.Empty:
            return  # nobody asked for a while: give the memory back
        if not line:
            return
        try:
            request = json.loads(line)
            reply = encoder.search(request["texts"], int(request.get("k", TOP_K)))
        except Exception as error:  # a bad request must not kill the worker
            reply = {"error": repr(error)}
        print(json.dumps(reply), flush=True)


# --- client (in the MCP server: standard library only) ---

class Client:
    """Starts the worker on demand and restarts it when it has exited for being idle.

    The worker runs with -I (isolated: the working directory, often the user's project, is not on
    its import path, so a stray numpy.py there is never run) from the data folder. Every read has a
    timeout, a worker that misbehaves is killed, and one that failed to start is not retried on
    every search.
    """

    def __init__(self, command: list[str] | None = None):
        self.command = command or [sys.executable, "-I", "-m", "sozai.semantic"]
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue[str] | None = None
        self.failed_at: float | None = None
        self.lock = threading.Lock()

    def _kill(self) -> None:
        if self.process is not None:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass
        self.process, self.lines = None, None

    def _readline(self, timeout: float) -> str:
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"semantic worker gave no answer within {timeout:.0f} s") from None

    def _start(self) -> None:
        if self.failed_at is not None and time.monotonic() - self.failed_at < RETRY_AFTER:
            raise RuntimeError("semantic worker failed to start recently")
        folder = paths.data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=folder)
        self.lines = lines = queue.Queue()
        stdout = self.process.stdout

        def read() -> None:
            for line in iter(stdout.readline, ""):
                lines.put(line)
            lines.put("")  # EOF: the worker exited

        threading.Thread(target=read, daemon=True).start()
        try:
            ready = json.loads(self._readline(START_TIMEOUT) or "{}").get("ready")
        except (ValueError, TimeoutError):
            ready = False
        if not ready:
            self._kill()
            self.failed_at = time.monotonic()
            raise RuntimeError("semantic worker failed to start")
        self.failed_at = None

    def search(self, texts: list[str], k: int = TOP_K) -> dict:
        with self.lock:
            for attempt in range(2):
                if self.process is None or self.process.poll() is not None:
                    self._kill()
                    self._start()
                try:
                    self.process.stdin.write(json.dumps({"texts": texts, "k": k}) + "\n")
                    self.process.stdin.flush()
                    line = self._readline(QUERY_TIMEOUT)
                except TimeoutError:  # before OSError, of which it is a subclass
                    self._kill()  # hung: never leave the server waiting on it again
                    raise
                except (BrokenPipeError, OSError):
                    line = ""
                if line:
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        self._kill()
                        raise RuntimeError("semantic worker sent a bad reply") from None
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    return reply
                self._kill()  # it exited between our check and the write: start a fresh one
            raise RuntimeError("semantic worker did not answer")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:
                self._kill()


_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client()
    return _client


if __name__ == "__main__":
    worker()
