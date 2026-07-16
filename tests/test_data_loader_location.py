"""Tests for blank-LOCATION rescue in core.data_loader.load_schedule.

A real ERP export sometimes ships planned units whose LOCATION cell is blank —
the scheduler simply didn't tag the row with a facility. Those rows used to be
dropped silently by the location filter, undercounting the plan (e.g. a
Henderson export where 24 real units had an empty LOCATION cell).

load_schedule now folds blank-LOCATION rows into the active facility so they're
counted, and records the rescue in ``df.attrs["location_recovered"]`` so the UI
can warn the planner. Rows tagged with a *different* explicit facility are still
excluded. These tests pin that behavior.
"""
import pandas as pd

from core.data_loader import load_schedule

SCHEDULE_COLS = [
    "LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY", "PRODUCTION MONTH",
]


def _write_schedule(tmp_path, rows):
    df = pd.DataFrame(rows, columns=SCHEDULE_COLS)
    p = tmp_path / "sched.csv"
    df.to_csv(p, index=False, encoding="latin-1")
    return p


def _row(loc, sku, month="Aug-26", qty="1"):
    return {
        "LOCATION": loc, "FG SKU ID": sku, "FG ACCRY SKU ID": "",
        "BUILD QTY": qty, "PRODUCTION MONTH": month,
    }


class TestBlankLocationRescue:
    def test_blank_location_rows_are_counted(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("HENDERSON", "SDG45-001"),
            _row("", "SDG65-006"),       # blank location — used to vanish
            _row("", "SDG65-006"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 3
        assert int(df["BUILD QTY"].sum()) == 3
        # rescued rows are re-tagged to the active facility
        assert (df["LOC"] == "HENDERSON").all()

    def test_summary_records_rescue(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("HENDERSON", "SDG45-001"),
            _row("", "SDG65-006", qty="2"),
            _row("", "SDG45-002", qty="3"),
        ])
        df = load_schedule(p, location="HENDERSON")
        rec = df.attrs["location_recovered"]
        assert rec["count"] == 5          # units (BUILD QTY), not rows
        assert rec["rows"] == 2
        assert rec["assigned_location"] == "HENDERSON"
        assert rec["by_fg"] == {"SDG65-006": 2, "SDG45-002": 3}

    def test_nan_location_cell_is_rescued(self, tmp_path):
        # A truly missing cell (NaN) counts as blank, same as empty string.
        rows = [
            _row("HENDERSON", "SDG45-001"),
            {"LOCATION": None, "FG SKU ID": "SDG65-006", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Aug-26"},
        ]
        df = _write_schedule_raw(tmp_path, rows)
        out = load_schedule(df, location="HENDERSON")
        assert len(out) == 2
        assert out.attrs["location_recovered"]["count"] == 1


class TestOtherFacilitiesUntouched:
    def test_all_tagged_location_reports_zero_rescue(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("HENDERSON", "SDG45-001"),
            _row("HENDERSON", "SDG45-002"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df.attrs["location_recovered"]["count"] == 0

    def test_different_facility_rows_excluded(self, tmp_path):
        # A row explicitly tagged for another plant must NOT be folded in.
        p = _write_schedule(tmp_path, [
            _row("HENDERSON", "SDG45-001"),
            _row("SPARTANBURG", "SDG45-002"),   # different plant → excluded
            _row("", "SDG65-006"),              # blank → rescued into HENDERSON
        ])
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 2                     # HENDERSON + blank, not SPARTANBURG
        assert "SDG45-002" not in df["FG_RAW"].tolist()
        rec = df.attrs["location_recovered"]
        assert rec["count"] == 1
        assert rec["by_fg"] == {"SDG65-006": 1}


def _write_schedule_raw(tmp_path, rows):
    """Like _write_schedule but preserves None/NaN cells (no column coercion)."""
    df = pd.DataFrame(rows)
    p = tmp_path / "sched_raw.csv"
    df.to_csv(p, index=False, encoding="latin-1")
    return p
