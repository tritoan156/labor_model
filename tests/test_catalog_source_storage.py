"""catalog_source.json — import provenance and the labor-minute lock."""
import json

import pytest

from core import catalog_source_storage as css


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Point the store at a scratch file; no GitHub."""
    path = tmp_path / "catalog_source.json"
    monkeypatch.setattr(css, "CATALOG_SOURCE_PATH", path)
    return path


@pytest.fixture
def github(monkeypatch):
    """A fake remote: what's 'on GitHub' plus every push, so the SHA-locked
    fetch-fresh-then-mutate behaviour is observable."""
    state = {"remote": None, "sha": "abc", "pushes": []}

    def fetch(file_path, token):
        if state["remote"] is None:
            return b"", None
        return json.dumps(state["remote"]).encode(), state["sha"]

    def save(content, file_path, token, message, sha=None):
        state["pushes"].append({"content": json.loads(content), "message": message, "sha": sha})
        state["remote"] = json.loads(content)
        return {"commit": {"sha": "deadbeef"}}

    monkeypatch.setattr(css, "fetch_catalog_csv_from_github", fetch)
    monkeypatch.setattr(css, "save_catalog_to_github", save)
    return state


class TestLoad:
    def test_missing_file_is_the_unlocked_default(self, local):
        assert css.load_catalog_source() == {"labor_locked": False, "imports": {}}
        assert css.is_labor_locked() is False
        assert css.last_import("machine") is None

    def test_garbage_file_is_the_default_too(self, local):
        local.write_text("{not json", encoding="utf-8")
        assert css.load_catalog_source() == {"labor_locked": False, "imports": {}}

    def test_unknown_keys_and_bad_types_are_dropped(self, local):
        local.write_text(json.dumps({
            "labor_locked": "yes",                      # truthy string → True
            "imports": {
                "machine": {"file": "a.xlsx", "cells": "7", "new_skus": None, "junk": 1},
                "accessory": {"file": ""},             # no file → not an import
                "widgets": {"file": "x.xlsx"},         # not a catalog
            },
            "something_else": 42,
        }), encoding="utf-8")
        got = css.load_catalog_source()
        assert got["labor_locked"] is True
        assert got["imports"] == {
            "machine": {"file": "a.xlsx", "sheet": "", "at": "", "cells": 7, "new_skus": 0},
        }
        assert "something_else" not in got


class TestRecordImport:
    def test_records_and_mirrors_locally(self, local, github):
        css.record_import_to_github(
            "machine", "CycleTimes.xlsx", "Machine", cells=47, new_skus=3,
            token="t", at="2026-10-21T14:02",
        )
        assert css.last_import("machine") == {
            "file": "CycleTimes.xlsx", "sheet": "Machine", "at": "2026-10-21T14:02",
            "cells": 47, "new_skus": 3,
        }
        push = github["pushes"][-1]
        assert "machine" in push["message"] and "47 cells" in push["message"]
        assert push["sha"] is None  # first write — file didn't exist remotely

    def test_defaults_at_to_now(self, local, github):
        css.record_import_to_github("accessory", "k.xlsx", "Acc", 1, 0, token="t")
        assert css.last_import("accessory")["at"][:4].isdigit()

    def test_unknown_catalog_rejected(self, local, github):
        with pytest.raises(ValueError):
            css.record_import_to_github("widgets", "k.xlsx", "S", 1, 0, token="t")


class TestWritesCompose:
    """A write is applied to what's on GitHub *now*, not to what this process
    read earlier — so two people changing different fields both land."""

    def test_import_keeps_a_lock_someone_else_set(self, local, github):
        github["remote"] = {"labor_locked": True, "imports": {}}
        css.record_import_to_github("machine", "a.xlsx", "M", 2, 0, token="t")
        assert github["remote"]["labor_locked"] is True
        assert github["remote"]["imports"]["machine"]["file"] == "a.xlsx"

    def test_lock_keeps_imports_someone_else_recorded(self, local, github):
        github["remote"] = {"labor_locked": False,
                            "imports": {"accessory": {"file": "k.xlsx", "cells": 5}}}
        css.set_labor_locked_to_github(True, token="t")
        assert github["remote"]["labor_locked"] is True
        assert github["remote"]["imports"]["accessory"]["file"] == "k.xlsx"
        assert css.is_labor_locked() is True

    def test_pushes_carry_the_fetched_sha(self, local, github):
        github["remote"] = {"labor_locked": False, "imports": {}}
        github["sha"] = "sha-1"
        css.set_labor_locked_to_github(True, token="t")
        assert github["pushes"][-1]["sha"] == "sha-1"


class TestConflictRetry:
    def test_retries_once_then_raises(self, local, monkeypatch):
        calls = {"n": 0}

        def fetch(file_path, token):
            return b"", None

        def save(*a, **k):
            calls["n"] += 1
            raise css.GitHubConflict("409")

        monkeypatch.setattr(css, "fetch_catalog_csv_from_github", fetch)
        monkeypatch.setattr(css, "save_catalog_to_github", save)
        with pytest.raises(css.GitHubConflict):
            css.set_labor_locked_to_github(True, token="t")
        assert calls["n"] == 2
