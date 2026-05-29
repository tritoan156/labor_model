"""Tests for the calculation helpers that live in app.py.

Importing app.py executes the Streamlit script in "bare" mode (it emits
ScriptRunContext warnings but loads). If that ever fails to import, these
tests skip rather than erroring the whole suite.
"""
import pandas as pd
import pytest

app = pytest.importorskip("app")


class TestFmtMinHr:
    def test_basic(self):
        assert app._fmt_min_hr(120) == "120 p-min · 2.0 p-hr"
        assert app._fmt_min_hr(0) == "0 p-min · 0.0 p-hr"

    def test_thousands_and_decimals(self):
        # p-min carries a thousands separator; p-hr does not (".1f" only).
        assert app._fmt_min_hr(164499) == "164,499 p-min · 2741.7 p-hr"

    def test_none_is_zero(self):
        assert app._fmt_min_hr(None) == "0 p-min · 0.0 p-hr"

    def test_nan_and_inf_degrade_gracefully(self):
        # Audit fix: NaN/Inf previously crashed int(round(...)).
        assert app._fmt_min_hr(float("nan")) == "0 p-min · 0.0 p-hr"
        assert app._fmt_min_hr(float("inf")) == "0 p-min · 0.0 p-hr"
        assert app._fmt_min_hr(float("-inf")) == "0 p-min · 0.0 p-hr"


class TestMaxUnitsAtCurrentMix:
    def _cap(self, rows):
        """rows: {station: (HC, labor_demand, need_per_day, labor_util, thru_util)}."""
        df = pd.DataFrame(
            [{"HC": hc, "labor_demand": ld, "need_per_day": npd,
              "labor_util_safe": lu, "thru_util_safe": tu}
             for (hc, ld, npd, lu, tu) in rows.values()],
            index=list(rows.keys()),
        )
        df.index.name = "station_display"
        return df

    def test_normal_caps(self):
        cap = self._cap({"QC": (4, 100.0, 5.0, 0.5, 0.25)})
        r = app._max_units_at_current_mix(cap, 100)
        assert r["potential_max_units"] == 200      # 100 / 0.5
        assert r["labor_max_units"] == 200          # 100 / 0.5
        assert r["thru_max_units"] == 400           # 100 / 0.25
        assert r["bottleneck"] == "QC"
        assert r["infeasible"] is False

    def test_infeasible_unstaffed(self):
        cap = self._cap({"QC": (0, 100.0, 5.0, 0.0, 0.0)})
        r = app._max_units_at_current_mix(cap, 100)
        assert r["infeasible"] is True
        assert "QC" in r["missing_stations"]

    def test_empty_returns_full_key_set(self):
        # Audit fix: the empty/zero early-return must carry the same keys as
        # the other branches so callers never KeyError.
        r = app._max_units_at_current_mix(pd.DataFrame(), 100)
        for k in ("labor_max_units", "thru_max_units", "potential_max_units",
                  "labor_max_util_safe", "thru_max_util_safe"):
            assert k in r
        assert r["max_units"] == 100

    def test_zero_units(self):
        r = app._max_units_at_current_mix(pd.DataFrame(), 0)
        assert r["max_units"] == 0
        assert "thru_max_units" in r
