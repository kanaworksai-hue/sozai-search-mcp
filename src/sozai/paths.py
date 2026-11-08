"""Where sozai keeps its index and caches."""
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def data_dir() -> Path:
    """The per-user data folder. SOZAI_HOME overrides it (tests, portable installs)."""
    override = os.environ.get("SOZAI_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "sozai"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "sozai"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "sozai"


def index_path() -> Path:
    return data_dir() / "index.sqlite"


_lock_depth = 0


@contextmanager
def data_lock(timeout: float = 300) -> Iterator[None]:
    """One sozai process at a time installs data or writes the index (two `sozai build` runs, or a
    build next to a session that updates its index). Re-entrant within a process."""
    global _lock_depth
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a+b") as handle:  # the OS drops the lock if the process dies
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"another sozai process is updating {folder}; try again later") from None
                time.sleep(0.2)
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
