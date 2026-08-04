"""Tests for absorbing rows tagged with a different facility.

A per-plant export can use LOCATION for where a unit is *headed* rather than
where it's *built* — the Henderson sheet carries rows tagged Spartanburg that
Henderson actually builds. ``absorb_foreign_locations`` folds those in instead
of excluding them.
"""
import pandas as pd
import pytest

from core.data_loader import load_schedule

COLS = ["LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY",
        "PRODUCTION MONTH", "MODEL TYPE"]


def _write(tmp_path, rows):
    p = tmp_path / "sched.csv"
    pd.DataFrame(rows, columns=COLS).to_csv(p, index=False, encoding="latin-1")
    return p


def _rows():
    return [
        {"LOCATION": "HENDERSON", "FG SKU ID": "A-1", "FG ACCRY SKU ID": "",
         "BUILD QTY": "2", "PRODUCTION MONTH": "Aug-26", "MODEL TYPE": "M1"},
        {"LOCATION": "Spartanburg", "FG SKU ID": "B-1", "FG ACCRY SKU ID": "",
         "BUILD QTY": "3", "PRODUCTION MONTH": "Aug-26", "MODEL TYPE": "M2"},
        {"LOCATION": "", "FG SKU ID": "C-1", "FG ACCRY SKU ID": "",
         "BUILD QTY": "1", "PRODUCTION MONTH": "Aug-26", "MODEL TYPE": "M3"},
    ]


class TestDefaultIsStrict:
    def test_foreign_rows_excluded_by_default(self, tmp_path):
        df = load_schedule(_write(tmp_path, _rows()), location="HENDERSON")
        # Henderson row (2) + untagged row (1) = 3; the Spartanburg row is out.
        assert int(df["BUILD QTY"].sum()) == 3
        assert "B-1" not in set(df["FG_RAW"])
        assert df.attrs["foreign_location_recovered"]["count"] == 0

    def test_blank_location_rescue_is_unaffected(self, tmp_path):
        df = load_schedule(_write(tmp_path, _rows()), location="HENDERSON")
        assert df.attrs["location_recovered"]["count"] == 1


class TestAbsorbForeign:
    def test_foreign_rows_are_counted_here(self, tmp_path):
        df = load_schedule(_write(tmp_path, _rows()), location="HENDERSON",
                           absorb_foreign_locations=True)
        assert int(df["BUILD QTY"].sum()) == 6      # 2 + 3 + 1
        assert "B-1" in set(df["FG_RAW"])

    def test_absorbed_rows_are_retagged_to_this_facility(self, tmp_path):
        df = load_schedule(_write(tmp_path, _rows()), location="HENDERSON",
                           absorb_foreign_locations=True)
        # Load-bearing: downstream grouping/display keys off LOC, so a row left
        # tagged Spartanburg would be counted here but shown under the wrong plant.
        assert set(df["LOC"]) == {"HENDERSON"}

    def test_summary_reports_what_moved(self, tmp_path):
        df = load_schedule(_write(tmp_path, _rows()), location="HENDERSON",
                           absorb_foreign_locations=True)
        f = df.attrs["foreign_location_recovered"]
        assert f["count"] == 3 and f["rows"] == 1
        assert f["assigned_location"] == "HENDERSON"
        assert f["by_location"] == {"SPARTANBURG": 3}
        assert f["by_fg"] == {"B-1": 3}

    def test_counts_units_not_rows(self, tmp_path):
        rows = _rows() + [
            {"LOCATION": "Cypress", "FG SKU ID": "D-1", "FG ACCRY SKU ID": "",
             "BUILD QTY": "5", "PRODUCTION MONTH": "Aug-26", "MODEL TYPE": "M4"},
        ]
        df = load_schedule(_write(tmp_path, rows), location="HENDERSON",
                           absorb_foreign_locations=True)
        f = df.attrs["foreign_location_recovered"]
        assert f["count"] == 8 and f["rows"] == 2
        assert f["by_location"] == {"CYPRESS": 5, "SPARTANBURG": 3}

    def test_no_foreign_rows_is_a_clean_noop(self, tmp_path):
        rows = [r for r in _rows() if r["LOCATION"] != "Spartanburg"]
        df = load_schedule(_write(tmp_path, rows), location="HENDERSON",
                           absorb_foreign_locations=True)
        assert int(df["BUILD QTY"].sum()) == 3
        assert df.attrs["foreign_location_recovered"]["count"] == 0
