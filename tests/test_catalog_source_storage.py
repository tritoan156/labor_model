"""catalog_source.json — import provenance, the Excel-first flag, and the
in-app minute edits that are pending in Excel."""
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


DEFAULT = {"excel_source_of_truth": False, "imports": {}, "pending": {}}


class TestLoad:
    def test_missing_file_is_the_default(self, local):
        assert css.load_catalog_source() == DEFAULT
        assert css.is_excel_source_of_truth() is False
        assert css.last_import("machine") is None
        assert css.pending_for("machine") == {}
        assert css.pending_count("machine") == 0

    def test_garbage_file_is_the_default_too(self, local):
        local.write_text("{not json", encoding="utf-8")
        assert css.load_catalog_source() == DEFAULT

    def test_unknown_keys_and_bad_types_are_dropped(self, local):
        local.write_text(json.dumps({
            "excel_source_of_truth": "yes",             # truthy string → True
            "imports": {
                "machine": {"file": "a.xlsx", "cells": "7", "new_skus": None, "junk": 1},
                "accessory": {"file": ""},             # no file → not an import
                "widgets": {"file": "x.xlsx"},         # not a catalog
            },
            "pending": {
                "machine": {
                    "BOSS25-010": {"FN_Assy": {"app_value": "150", "excel_value": 120},
                                   "PDI": {"excel_value": 60}},        # no app value → noise
                    "": {"QC": {"app_value": 1}},                        # blank SKU
                },
                "widgets": {"X": {"Y": {"app_value": 1}}},
            },
            "something_else": 42,
        }), encoding="utf-8")
        got = css.load_catalog_source()
        assert got["excel_source_of_truth"] is True
        assert got["imports"] == {
            "machine": {"file": "a.xlsx", "sheet": "", "at": "", "cells": 7, "new_skus": 0},
        }
        assert got["pending"] == {
            "machine": {"BOSS25-010": {"FN_Assy": {"app_value": 150.0, "excel_value": 120.0, "at": ""}}},
        }
        assert "something_else" not in got

    def test_pending_with_no_excel_value_is_kept(self, local):
        # An override on a cell Excel left blank has nothing to compare to —
        # still a real pending change.
        local.write_text(json.dumps({"pending": {"accessory": {
            "K-1": {"GenAcc": {"app_value": 145, "excel_value": None}},
        }}}), encoding="utf-8")
        assert css.pending_for("accessory")["K-1"]["GenAcc"]["excel_value"] is None


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


class TestPending:
    def test_record_then_read(self, local, github):
        css.record_pending_to_github(
            "machine", [("BOSS25-010", "FN_Assy", 150, 120), ("BOSS70-002", "PDI", 90, 60)],
            token="t", at="2026-10-21T15:00",
        )
        assert css.pending_for("machine") == {
            "BOSS25-010": {"FN_Assy": {"app_value": 150.0, "excel_value": 120.0, "at": "2026-10-21T15:00"}},
            "BOSS70-002": {"PDI": {"app_value": 90.0, "excel_value": 60.0, "at": "2026-10-21T15:00"}},
        }
        assert css.pending_count("machine") == 2
        assert "2 temporary machine" in github["pushes"][-1]["message"]

    def test_re_editing_keeps_the_original_excel_value(self, local, github):
        # 120 (Excel) → 150 (app) → 160 (app again). Reconciliation must still
        # compare against 120, the value the workbook is presumed to hold.
        css.record_pending_to_github("machine", [("B", "FN_Assy", 150, 120)], token="t")
        css.record_pending_to_github("machine", [("B", "FN_Assy", 160, 150)], token="t")
        rec = css.pending_for("machine")["B"]["FN_Assy"]
        assert rec["app_value"] == 160.0 and rec["excel_value"] == 120.0

    def test_blank_excel_value_is_allowed(self, local, github):
        css.record_pending_to_github("accessory", [("K", "GenAcc", 145, None)], token="t")
        assert css.pending_for("accessory")["K"]["GenAcc"]["excel_value"] is None

    def test_nothing_valid_makes_no_push(self, local, github):
        assert css.record_pending_to_github("machine", [("", "X", 1, 0), ("S", "", 1, 0),
                                                        ("S", "X", None, 0)], token="t") == {}
        assert github["pushes"] == []

    def test_clear_some_leaves_the_rest(self, local, github):
        css.record_pending_to_github(
            "machine", [("B", "FN_Assy", 150, 120), ("B", "PDI", 90, 60), ("C", "QC", 40, 30)],
            token="t",
        )
        css.clear_pending_to_github("machine", [("B", "FN_Assy"), ("C", "QC")], token="t",
                                    reason="Excel caught up")
        assert css.pending_for("machine") == {
            "B": {"PDI": {"app_value": 90.0, "excel_value": 60.0, "at": css.pending_for("machine")["B"]["PDI"]["at"]}},
        }
        assert "Excel caught up" in github["pushes"][-1]["message"]

    def test_clear_unknown_keys_is_harmless(self, local, github):
        css.record_pending_to_github("machine", [("B", "FN_Assy", 150, 120)], token="t")
        css.clear_pending_to_github("machine", [("ZZZ", "Nope")], token="t")
        assert css.pending_count("machine") == 1

    def test_clear_nothing_makes_no_push(self, local, github):
        assert css.clear_pending_to_github("machine", [], token="t") == {}


class TestWritesCompose:
    """A write is applied to what's on GitHub *now*, not to what this process
    read earlier — so two people changing different fields both land."""

    def test_import_keeps_a_flag_someone_else_set(self, local, github):
        github["remote"] = {"excel_source_of_truth": True, "imports": {}}
        css.record_import_to_github("machine", "a.xlsx", "M", 2, 0, token="t")
        assert github["remote"]["excel_source_of_truth"] is True
        assert github["remote"]["imports"]["machine"]["file"] == "a.xlsx"

    def test_flag_keeps_imports_and_pending_someone_else_recorded(self, local, github):
        github["remote"] = {
            "excel_source_of_truth": False,
            "imports": {"accessory": {"file": "k.xlsx", "cells": 5}},
            "pending": {"machine": {"B": {"FN_Assy": {"app_value": 150, "excel_value": 120}}}},
        }
        css.set_excel_source_to_github(True, token="t")
        assert github["remote"]["excel_source_of_truth"] is True
        assert github["remote"]["imports"]["accessory"]["file"] == "k.xlsx"
        assert github["remote"]["pending"]["machine"]["B"]["FN_Assy"]["app_value"] == 150
        assert css.is_excel_source_of_truth() is True

    def test_pending_on_one_catalog_keeps_the_other(self, local, github):
        github["remote"] = {"pending": {"accessory": {"K": {"GenAcc": {"app_value": 1}}}}}
        css.record_pending_to_github("machine", [("B", "PDI", 90, 60)], token="t")
        assert set(github["remote"]["pending"]) == {"accessory", "machine"}

    def test_pushes_carry_the_fetched_sha(self, local, github):
        github["remote"] = {"excel_source_of_truth": False}
        github["sha"] = "sha-1"
        css.set_excel_source_to_github(True, token="t")
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
            css.set_excel_source_to_github(True, token="t")
        assert calls["n"] == 2
