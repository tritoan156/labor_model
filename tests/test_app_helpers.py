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


class TestAreaSpaceCap:
    def _cap(self, rows):
        """rows: {station: (HC, need_per_day, thru_cap_safe, thru_util_safe)}."""
        df = pd.DataFrame(
            [{"HC": hc, "need_per_day": npd, "thru_cap_safe": cap,
              "thru_util_safe": tu}
             for (hc, npd, cap, tu) in rows.values()],
            index=list(rows.keys()),
        )
        df.index.name = "station_display"
        return df

    def test_limiting_cell_is_highest_util(self):
        # Use exactly-representable utils (0.25, 0.5) so the floor is unambiguous.
        cap = self._cap({"A": (4, 2.5, 10.0, 0.25), "B": (4, 5.0, 10.0, 0.5)})
        r = app._area_space_cap(cap, 100)
        assert r["limiting_cell"] == "B"      # higher util = binding cell
        assert r["cells_used_pct"] == 50
        assert r["max_units"] == 200          # floor(100 / 0.5)

    def test_ignores_hc_for_space_bottleneck(self):
        # The whole point of the fix: a station with NO staff (HC=0) but real
        # cells + demand must still be the area's space bottleneck. The
        # labor-aware helper would wrongly skip it.
        cap = self._cap({"A": (4, 2.5, 10.0, 0.25), "B": (0, 5.0, 10.0, 0.5)})
        r = app._area_space_cap(cap, 100)
        assert r["limiting_cell"] == "B"      # HC=0 but still binding
        assert r["max_units"] == 200          # floor(100 / 0.5)
        # Contrast: the labor-aware helper skips the HC=0 station → wrong cell.
        m = app._max_units_at_current_mix(cap, 100)
        assert m["thru_bottleneck"] == "A"

    def test_none_when_no_space_demand(self):
        assert app._area_space_cap(self._cap({"A": (4, 0.0, 10.0, 0.0)}), 100) is None
        assert app._area_space_cap(pd.DataFrame(), 100) is None
        assert app._area_space_cap(self._cap({"A": (4, 5.0, 10.0, 0.5)}), 0) is None
