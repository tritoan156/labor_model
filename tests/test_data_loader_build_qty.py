"""Regression tests for BUILD QTY coercion in core.data_loader.load_schedule.

A real ERP export can carry a BUILD QTY cell that ``pd.to_numeric`` coerces to
``±inf`` — a literal ``"inf"``/``"Infinity"`` string, or a number so large it
overflows float64 (e.g. ``"1e400"``). ``+inf`` is not NaN and satisfies
``> 0``, so it used to slip through the ``notna() & > 0`` filter and then crash
``.astype(int)`` with ``IntCastingNaNError: Cannot convert non-finite values
(NA or inf) to integer`` — taking the entire upload down. These tests pin the
fix: non-finite quantities are dropped like any other invalid row, and the
valid rows still load.
"""
import pandas as pd

from core.data_loader import load_schedule

SCHEDULE_COLS = [
    "LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY", "PRODUCTION MONTH",
]


def _write_schedule(tmp_path, rows):
    """Write a minimal schedule CSV (latin-1, like a real ERP export)."""
    df = pd.DataFrame(rows, columns=SCHEDULE_COLS)
    p = tmp_path / "sched.csv"
    df.to_csv(p, index=False, encoding="latin-1")
    return p


def _row(sku, qty):
    return {
        "LOCATION": "HENDERSON", "FG SKU ID": sku, "FG ACCRY SKU ID": "",
        "BUILD QTY": qty, "PRODUCTION MONTH": "Jul-26",
    }


class TestNonFiniteBuildQty:
    def test_inf_string_row_is_dropped_not_crashed(self, tmp_path):
        # The row that used to raise IntCastingNaNError must be silently
        # dropped, and the valid rows around it must still load.
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "1"),
            _row("BOSS25-011", "inf"),
            _row("BOSS25-012", "2"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 2
        assert set(df["FG SKU ID"]) == {"BOSS25-010", "BOSS25-012"}
        assert df["BUILD QTY"].tolist() == [1, 2]

    def test_negative_inf_is_dropped(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "-inf"),
            _row("BOSS25-011", "3"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-011"]
        assert df["BUILD QTY"].tolist() == [3]

    def test_float_overflow_row_is_dropped(self, tmp_path):
        # A number too large for float64 coerces to +inf, not NaN.
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "1e400"),
            _row("BOSS25-011", "5"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-011"]

    def test_infinity_word_mixed_case_is_dropped(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "Infinity"),
            _row("BOSS25-011", "7"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["BUILD QTY"].tolist() == [7]

    def test_all_rows_non_finite_yields_empty_frame_no_crash(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "inf"),
            _row("BOSS25-011", "-inf"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 0

    def test_build_qty_is_integer_dtype(self, tmp_path):
        # After coercion the surviving BUILD QTY column must be a real int, not
        # a float carrying the ghost of the dropped inf rows.
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "1"),
            _row("BOSS25-011", "inf"),
            _row("BOSS25-012", "4"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert pd.api.types.is_integer_dtype(df["BUILD QTY"])


class TestOrdinaryBuildQtyUnaffected:
    def test_plain_integers_preserved(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "1"),
            _row("BOSS25-011", "2"),
            _row("BOSS25-012", "3"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["BUILD QTY"].tolist() == [1, 2, 3]

    def test_zero_and_negative_still_dropped(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "0"),
            _row("BOSS25-011", "-3"),
            _row("BOSS25-012", "4"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["BUILD QTY"].tolist() == [4]

    def test_fractional_qty_truncates_as_before(self, tmp_path):
        # .astype(int) truncates toward zero; the fix must not change that.
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "2.9"),
            _row("BOSS25-011", "3.2"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["BUILD QTY"].tolist() == [2, 3]

    def test_non_numeric_junk_still_dropped(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "abc"),
            _row("BOSS25-011", ""),
            _row("BOSS25-012", "5"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["BUILD QTY"].tolist() == [5]


class TestBlankFgSku:
    """Audit fix: a whitespace-only FG SKU cell must be dropped like a truly
    empty (NaN) one. `.notna()` alone kept a `" "` row, which then normalized to
    an empty FG SKU — a phantom unit counted in the plan total but resolving to
    no catalog SKU (zero labor, not even reported as skipped)."""

    def test_whitespace_sku_dropped_like_nan(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("BOSS25-010", "2"),
            _row("   ", "5"),        # whitespace-only SKU — must be dropped
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-010"]
        assert int(df["BUILD QTY"].sum()) == 2

    def test_empty_string_sku_dropped(self, tmp_path):
        p = _write_schedule(tmp_path, [
            _row("", "5"),
            _row("BOSS25-011", "3"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-011"]

    def test_real_skus_with_surrounding_space_survive_stripped(self, tmp_path):
        # A real SKU that merely carries stray padding is still a valid unit —
        # it must be kept (and downstream-stripped), not dropped.
        p = _write_schedule(tmp_path, [
            _row(" BOSS25-010 ", "4"),
        ])
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 1
        assert df["FG_RAW"].tolist() == ["BOSS25-010"]
