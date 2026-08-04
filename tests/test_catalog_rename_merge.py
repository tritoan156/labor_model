"""Tests for the SKU-rename collision → typo-merge path in app.py.

Renaming a row onto a SKU that already exists used to be a flat rejection.
When the two rows really are the same product (one SKU is a typo), the planner
can now confirm the merge: the row already in the catalog is kept untouched and
the duplicate is deleted.
"""
import io

import pandas as pd
import pytest

from app import (
    _apply_diffs_and_serialize,
    _rename_collisions,
    _validate_renames,
)


def _catalog():
    return pd.DataFrame([
        {"SKU": "BOSS25-10",  "Description": "EBOSS 25-25 TYPO ROW", "Trailer": 999, "Last Modified": ""},
        {"SKU": "BOSS25-010", "Description": "EBOSS 25-25 GOOD ROW", "Trailer": 120, "Last Modified": ""},
        {"SKU": "SDG45-001",  "Description": "SDG45S",               "Trailer": 90,  "Last Modified": ""},
    ])


class TestRenameCollisions:
    def test_detects_collision_with_existing_sku(self):
        assert _rename_collisions(
            [("BOSS25-10", "BOSS25-010")], _catalog()
        ) == [("BOSS25-10", "BOSS25-010")]

    def test_free_target_is_not_a_collision(self):
        assert _rename_collisions([("BOSS25-10", "BOSS25-011")], _catalog()) == []

    def test_case_insensitive(self):
        assert _rename_collisions([("BOSS25-10", "boss25-010")], _catalog()) == [
            ("BOSS25-10", "boss25-010")
        ]

    def test_no_op_rename_ignored(self):
        assert _rename_collisions([("SDG45-001", "SDG45-001")], _catalog()) == []


class TestValidateRenames:
    def test_collision_rejected_without_approval(self):
        clean, merges, err = _validate_renames(
            [("BOSS25-10", "BOSS25-010")], _catalog(),
        )
        assert clean is None and merges is None
        assert "already exists" in err

    def test_collision_becomes_a_merge_when_approved(self):
        clean, merges, err = _validate_renames(
            [("BOSS25-10", "BOSS25-010")], _catalog(),
            merge_approved={"BOSS25-10"},
        )
        assert err is None
        assert clean == []                              # not an ordinary rename
        assert merges == [("BOSS25-10", "BOSS25-010")]  # folded into the existing row

    def test_approval_is_case_insensitive(self):
        _clean, merges, err = _validate_renames(
            [("BOSS25-10", "BOSS25-010")], _catalog(),
            merge_approved={"boss25-10"},
        )
        assert err is None and merges == [("BOSS25-10", "BOSS25-010")]

    def test_approval_does_not_excuse_other_errors(self):
        # A blank target is still invalid no matter what was approved.
        _clean, _merges, err = _validate_renames(
            [("BOSS25-10", "")], _catalog(), merge_approved={"BOSS25-10"},
        )
        assert "empty value" in err

    def test_approval_does_not_excuse_formula_prefix(self):
        _clean, _merges, err = _validate_renames(
            [("BOSS25-10", "=CMD")], _catalog(), merge_approved={"BOSS25-10"},
        )
        assert "formula execution" in err

    def test_non_colliding_rename_still_a_plain_rename(self):
        clean, merges, err = _validate_renames(
            [("BOSS25-10", "BOSS25-011")], _catalog(),
            merge_approved={"BOSS25-10"},
        )
        assert err is None
        assert clean == [("BOSS25-10", "BOSS25-011")]
        assert merges == []


class TestApplyMerge:
    def _roundtrip(self, csv_text):
        return pd.read_csv(io.StringIO(csv_text))

    def test_merge_drops_duplicate_and_keeps_existing_row(self):
        csv_text, _n_cells, n_ren, n_merged = _apply_diffs_and_serialize(
            _catalog(), [], ["Trailer"],
            renames=[], merges=[("BOSS25-10", "BOSS25-010")],
        )
        out = self._roundtrip(csv_text)
        assert n_merged == 1 and n_ren == 0
        assert "BOSS25-10" not in set(out["SKU"])
        # The surviving row is the one that was already on file — untouched.
        kept = out[out["SKU"] == "BOSS25-010"].iloc[0]
        assert kept["Trailer"] == 120
        assert kept["Description"] == "EBOSS 25-25 GOOD ROW"
        assert len(out) == 2

    def test_merge_never_copies_the_duplicates_values_over(self):
        # Load-bearing: the planner said the EXISTING row is the right one, so
        # the typo row's labor (999) must not survive the merge.
        csv_text, *_ = _apply_diffs_and_serialize(
            _catalog(), [("BOSS25-10", "Trailer", 777)], ["Trailer"],
            renames=[], merges=[("BOSS25-10", "BOSS25-010")],
        )
        out = self._roundtrip(csv_text)
        assert out[out["SKU"] == "BOSS25-010"].iloc[0]["Trailer"] == 120
        assert 777 not in set(out["Trailer"])

    def test_renames_and_merges_coexist(self):
        csv_text, _n_cells, n_ren, n_merged = _apply_diffs_and_serialize(
            _catalog(), [], ["Trailer"],
            renames=[("SDG45-001", "SDG45-002")],
            merges=[("BOSS25-10", "BOSS25-010")],
        )
        out = self._roundtrip(csv_text)
        assert n_ren == 1 and n_merged == 1
        assert set(out["SKU"]) == {"BOSS25-010", "SDG45-002"}

    def test_no_merges_leaves_catalog_intact(self):
        csv_text, _n_cells, _n_ren, n_merged = _apply_diffs_and_serialize(
            _catalog(), [], ["Trailer"],
        )
        out = self._roundtrip(csv_text)
        assert n_merged == 0
        assert len(out) == 3
