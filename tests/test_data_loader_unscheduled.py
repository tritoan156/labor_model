"""Tests for undated-unit rescue in core.data_loader.load_schedule.

A real ERP export routinely ships planned units whose PRODUCTION MONTH cell is
blank or holds a data-entry note like "NEED SN/QA" — the scheduler simply
hasn't dated them yet. Those rows used to be dropped silently by the month
filter, so an upload of 145 units would report only the 119 that happened to
carry a recognizable ``Mon-YY`` month.

load_schedule now folds such rows into the current plan month so they're
counted, and records the rescue in ``df.attrs["unscheduled_recovered"]`` so the
UI can warn the planner. These tests pin that behavior — and that genuinely
dated rows (carryover ``YY-Mon`` and other real months) are left untouched.
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


def _row(sku, month, qty="1"):
    return {
        "LOCATION": "CYPRESS", "FG SKU ID": sku, "FG ACCRY SKU ID": "",
        "BUILD QTY": qty, "PRODUCTION MONTH": month,
    }


class TestUndatedUnitRescue:
    def test_blank_month_rows_are_counted_in_plan_month(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG65-006", ""),      # blank month — used to vanish
            _row("SDG65-006", ""),
        ])
        df = load_schedule(p, location="CYPRESS")
        assert len(df) == 3
        assert (df["PRODUCTION MONTH"] == "Aug-26").all()
        assert int(df["BUILD QTY"].sum()) == 3

    def test_unrecognized_note_rows_are_counted(self, tmp_path):
        # A literal "NEED SN/QA" in the month cell is not a date, but the unit
        # is real and must be counted, not dropped.
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG45-015", "NEED SN/QA"),
        ])
        df = load_schedule(p, location="CYPRESS")
        assert len(df) == 2
        assert (df["PRODUCTION MONTH"] == "Aug-26").all()

    def test_summary_records_reason_breakdown(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG65-006", ""),
            _row("SDG65-006", ""),
            _row("SDG45-015", "NEED SN/QA"),
        ])
        df = load_schedule(p, location="CYPRESS")
        rec = df.attrs["unscheduled_recovered"]
        assert rec["count"] == 3
        assert rec["rows"] == 3
        assert rec["assigned_month"] == "Aug-26"
        assert rec["by_reason"] == {"(blank)": 2, "NEED SN/QA": 1}

    def test_rescued_rows_are_not_carryover(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG65-006", ""),
        ])
        df = load_schedule(p, location="CYPRESS")
        assert not df["CARRYOVER"].any()

    def test_build_qty_summed_not_row_counted(self, tmp_path):
        # count reflects units (BUILD QTY), not just row count.
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26", qty="2"),
            _row("SDG65-006", "", qty="5"),
        ])
        df = load_schedule(p, location="CYPRESS")
        rec = df.attrs["unscheduled_recovered"]
        assert rec["count"] == 5   # units, from the one blank-month row
        assert rec["rows"] == 1


class TestDatedRowsUntouched:
    def test_all_dated_schedule_reports_zero_unscheduled(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG45-002", "Aug-26"),
        ])
        df = load_schedule(p, location="CYPRESS")
        assert df.attrs["unscheduled_recovered"]["count"] == 0
        assert (df["PRODUCTION MONTH"] == "Aug-26").all()

    def test_carryover_rows_not_reassigned_to_plan_month(self, tmp_path):
        # A YY-Mon carryover row is genuinely dated — it must stay on its own
        # month (kept as carryover), never folded into the current plan month.
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG45-002", "26-Jul"),   # carryover
            _row("SDG65-006", ""),         # orphan → Aug-26
        ])
        df = load_schedule(p, location="CYPRESS", include_carryover=True)
        months = df["PRODUCTION MONTH"].tolist()
        assert "26-Jul" in months            # carryover preserved, not moved
        assert months.count("Aug-26") == 2   # one real + one rescued orphan
        rec = df.attrs["unscheduled_recovered"]
        assert rec["count"] == 1             # only the blank row was rescued
        assert rec["by_reason"] == {"(blank)": 1}

    def test_carryover_excluded_but_orphan_still_rescued(self, tmp_path):
        # With include_carryover=False the carryover row drops, but the blank
        # orphan is still rescued into the plan month.
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "Aug-26"),
            _row("SDG45-002", "26-Jul"),
            _row("SDG65-006", ""),
        ])
        df = load_schedule(p, location="CYPRESS", include_carryover=False)
        assert "26-Jul" not in df["PRODUCTION MONTH"].tolist()
        assert (df["PRODUCTION MONTH"] == "Aug-26").all()
        assert len(df) == 2

    def test_multiple_current_months_anchor_to_dominant(self, tmp_path):
        # Two current months present; the orphan folds into whichever has more
        # rows (Aug-26 here), not the minority month.
        rows = [_row(f"SDG45-{i:03d}", "Aug-26") for i in range(3)]
        rows += [_row("SDG45-100", "Sep-26")]
        rows += [_row("SDG65-006", "")]  # orphan
        p = _write_schedule(tmp_path, rows)
        df = load_schedule(p, location="CYPRESS")
        rec = df.attrs["unscheduled_recovered"]
        assert rec["assigned_month"] == "Aug-26"
        assert df["PRODUCTION MONTH"].tolist().count("Aug-26") == 4
        assert df["PRODUCTION MONTH"].tolist().count("Sep-26") == 1


class TestCarryoverOnlySliceKeepsOrphans:
    """Audit fix: when a slice has carryover months but NO current-format month
    to anchor undated rows to, those undated rows must be KEPT (and surfaced),
    not silently dropped. Previously the orphan-rescue block was skipped because
    the anchor was "", and the `isin(months)` filter then removed every undated
    row with count=0 — a silent undercount driven only by the absence of an
    unrelated current-month row.
    """

    def test_undated_rows_kept_when_no_current_month(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "26-Jul", qty="3"),   # carryover only
            _row("SDG65-006", "NEED SN/QA", qty="5"),
            _row("SDG65-006", "", qty="7"),          # blank
        ])
        df = load_schedule(p, location="CYPRESS", include_carryover=True)
        # All 15 units survive (3 carryover + 12 undated), not just the 3 dated.
        assert int(df["BUILD QTY"].sum()) == 15
        assert len(df) == 3

    def test_summary_flags_unplaceable_orphans(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "26-Jul", qty="3"),
            _row("SDG65-006", "NEED SN/QA", qty="5"),
            _row("SDG65-006", "", qty="7"),
        ])
        df = load_schedule(p, location="CYPRESS", include_carryover=True)
        rec = df.attrs["unscheduled_recovered"]
        assert rec["count"] == 12          # 5 + 7 undated units
        assert rec["rows"] == 2
        assert rec["assigned_month"] == ""  # nothing to anchor to → kept undated
        assert rec["by_reason"] == {"NEED SN/QA": 1, "(blank)": 1}

    def test_adding_a_current_month_row_flips_to_rescue(self, tmp_path):
        # Same undated rows, but now with one current-month row present: the
        # orphans are rescued into that month instead of kept undated. Guards
        # the boundary between the two code paths.
        p = _write_schedule(tmp_path, [
            _row("SDG45-001", "26-Jul", qty="3"),
            _row("SDG65-006", "NEED SN/QA", qty="5"),
            _row("SDG65-006", "", qty="7"),
            _row("SDG45-002", "Aug-26", qty="2"),   # a current month appears
        ])
        df = load_schedule(p, location="CYPRESS", include_carryover=True)
        assert int(df["BUILD QTY"].sum()) == 17
        rec = df.attrs["unscheduled_recovered"]
        assert rec["assigned_month"] == "Aug-26"
        assert rec["count"] == 12
