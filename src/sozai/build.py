"""Build the local index from the libraries' public APIs."""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from sozai import index, paths
from sozai.http import Http


def build_index(path: Path | None = None, only: list[str] | None = None,
                log: Callable[[str], None] | None = None, live: bool = False, refresh: bool = True) -> dict:
    """Write the index. Returns {"count", "sources", "path", "data"}.

    By default the records come from the published data zip (see bundle.py), downloaded once and
    again whenever this sozai release expects a newer one; live=True reads every library's API
    instead (maintainers, or for the newest assets). Either way, kits the user ingested on this
    machine replace their published records. refresh=False keeps whatever data is installed (it
    was just installed by hand with `sozai build --data`).
    """
    with paths.data_lock():  # another process must not swap the data folder while we read it
        return _build_index(path, only, log, live, refresh)


def _build_index(path: Path | None, only: list[str] | None, log: Callable[[str], None] | None,
                 live: bool, refresh: bool) -> dict:
    from sozai import bundle

    path = path or paths.index_path()
    log = log or (lambda message: print(message, file=sys.stderr))
    records: list[dict] = []
    counts: dict[str, int] = {}
    data = None
    if not live:
        data = bundle.installed()
        if data is None or (refresh and bundle.outdated(data)):
            try:
                data = bundle.install(log=log)
            except Exception as error:  # no release yet, offline...
                if data:  # keep searching the older data rather than nothing
                    log(f"sozai: could not get published data {bundle.DATA_VERSION} ({error}); "
                        f"keeping {data['data_version']}")
                else:  # fall back to the libraries' APIs
                    log(f"sozai: published data unavailable ({error}); reading the libraries directly")
    if data:
        for record in bundle.load_records():
            if not only or record["source"] in only:
                records.append(record)
                counts[record["source"]] = counts.get(record["source"], 0) + 1
        log(f"sozai: published data {data['data_version']}: {len(records)} assets")
    else:
        from sozai.sources import ALL

        http = Http()
        for name, module in ALL.items():
            if only and name not in only:
                continue
            log(f"sozai: reading {name}...")
            before = len(records)
            records.extend(module.fetch(http))
            counts[name] = len(records) - before
            log(f"sozai: {name}: {counts[name]} assets")
    from sozai.catalog import load_catalogs

    for name, rows in load_catalogs().items():  # kits ingested on this machine (`sozai ingest`)
        if only and name not in only:
            continue
        local = {row["id"] for row in rows}
        records = [r for r in records if r["id"] not in local] + rows
        counts[name] = sum(1 for r in records if r["source"] == name)
        log(f"sozai: {name}: {len(rows)} assets from local kits")
    count = index.build(records, path, sources=counts)
    return {"count": count, "sources": counts, "path": str(path),
            "data": data["data_version"] if data else "live"}
