"""The published data zip: packed without local paths, checked on install, overridden by local kits."""
import shutil
import zipfile
from contextlib import closing

import pytest

from sozai import bundle, catalog, index
from sozai.build import build_index
from sozai.search import Searcher

from test_fetch import make_kit


def test_pack_install_build_round_trip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SOZAI_HOME", str(home))
    kits = tmp_path / "kits"
    kits.mkdir()
    make_kit(kits)
    catalog.ingest("kenney", kits, log=lambda m: None)
    build_index(log=lambda m: None, live=True, only=["kenney"])
    packed = bundle.pack(tmp_path / "dist", version="test", log=lambda m: None)
    assert packed["assets"] == 2 and packed["sources"] == {"kenney": 2}

    # a user without the kit: published records only, nothing pointing at the maintainer's disk
    shutil.rmtree(home / "catalog")
    bundle.install(packed["path"], log=lambda m: None)
    built = build_index(log=lambda m: None)
    assert built["data"] == "test" and built["count"] == 2
    with closing(index.connect(home / "index.sqlite")) as conn:  # Windows cannot replace an open index
        crate = Searcher(conn).get("kenney:test-kit/crate-wide")
    assert crate["acquire"] == "human" and crate["thumbnail_url"] is None
    assert set(crate["downloads"]) == {"kit_page", "member"} and "sozai ingest" in crate["how_to_get"]

    # the user ingests the kit: their local records win
    catalog.ingest("kenney", kits, log=lambda m: None)
    build_index(log=lambda m: None)
    with closing(index.connect(home / "index.sqlite")) as conn:  # Windows cannot replace an open index
        crate = Searcher(conn).get("kenney:test-kit/crate-wide")
    assert crate["acquire"] == "local" and crate["thumbnail_url"].startswith("file:")


def test_install_rejects_damaged_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    index.build([], tmp_path / "home" / "index.sqlite")
    packed = bundle.pack(tmp_path / "dist", version="x", log=lambda m: None)
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(packed["path"]) as src, zipfile.ZipFile(damaged, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            dst.writestr(name, data + b"x" if name == bundle.RECORDS else data)
    with pytest.raises(ValueError, match="damaged"):
        bundle.install(str(damaged), log=lambda m: None)
    assert bundle.installed() is None


def test_build_falls_back_to_live_when_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))

    def offline(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(bundle, "install", offline)
    logs = []
    built = build_index(log=logs.append, only=["kenney"])
    assert built["data"] == "live" and any("unavailable" in line for line in logs)


def _install_version(tmp_path, version):
    index.build([], tmp_path / "home" / "index.sqlite")
    packed = bundle.pack(tmp_path / f"dist-{version}", version=version, log=lambda m: None)
    bundle.install(packed["path"], log=lambda m: None)


def test_outdated_compares_date_versions(monkeypatch):
    monkeypatch.setattr(bundle, "DATA_VERSION", "2026.11.08")
    assert bundle.outdated(None)
    assert bundle.outdated({"data_version": "2026.10.30"})
    assert not bundle.outdated({"data_version": "2026.11.08"})
    assert not bundle.outdated({"data_version": "2026.12.01"})  # newer than this release knows
    assert not bundle.outdated({"data_version": "my-own"})  # installed by hand


def test_build_refreshes_outdated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    _install_version(tmp_path, "2026.01.01")
    monkeypatch.setattr(bundle, "DATA_VERSION", "2026.11.08")
    calls = []
    real_install = bundle.install

    def install_new(*args, **kwargs):
        calls.append(args)
        index.build([], tmp_path / "home" / "index.sqlite")
        packed = bundle.pack(tmp_path / "dist-new", version="2026.11.08", log=lambda m: None)
        return real_install(packed["path"], log=lambda m: None)

    monkeypatch.setattr(bundle, "install", install_new)
    assert build_index(log=lambda m: None)["data"] == "2026.11.08" and len(calls) == 1
    assert build_index(log=lambda m: None)["data"] == "2026.11.08" and len(calls) == 1  # up to date now


def test_build_keeps_old_data_when_refresh_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    _install_version(tmp_path, "2026.01.01")
    monkeypatch.setattr(bundle, "DATA_VERSION", "2026.11.08")

    def offline(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(bundle, "install", offline)
    logs = []
    assert build_index(log=logs.append)["data"] == "2026.01.01"
    assert any("keeping 2026.01.01" in line for line in logs)


def test_build_without_refresh_keeps_hand_installed_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOZAI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    _install_version(tmp_path, "2026.01.01")
    monkeypatch.setattr(bundle, "DATA_VERSION", "2026.11.08")
    monkeypatch.setattr(bundle, "install", lambda *a, **k: pytest.fail("should not download"))
    assert build_index(log=lambda m: None, refresh=False)["data"] == "2026.01.01"
