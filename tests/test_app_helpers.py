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


class TestWeeklyUtilizationPartialWeeks:
    """Audit fix: _compute_weekly_utilization used a hardcoded 5-day week for
    every WEEK_OF_MONTH bucket, so a partial leading/trailing calendar week (1–4
    working days) had its capacity overstated and its utilization understated —
    painting a physically overloaded short week green. It must divide each
    week's demand by that week's TRUE count of M–F working days.
    """

    def _units_on(self, machine_df, acc_df, days_qty):
        # Build a full expanded-ready schedule with real PRODUCTION DAY dates.
        from core.constants import week_of_month
        rows = []
        for day, qty in days_qty:
            ts = pd.Timestamp(day)
            for _ in range(qty):
                rows.append({
                    "FG_RAW": "BOSS25-010", "FG_BASE": "BOSS25-010",
                    "ACC": "BOSS25-A016", "BUILD QTY": 1, "CARRYOVER": False,
                    "DECAL": "", "CUSTOMER NAME": "", "PRODUCTION DAY": ts,
                    "WEEK_OF_MONTH": week_of_month(ts.date()),
                })
        return pd.DataFrame(rows)

    def _inputs(self):
        crew = pd.DataFrame(
            {"HC": [3, 4], "Conc": [4, 4], "Crew": [1, 1]},
            index=["PM Acc (Headunit)", "PDI"],
        )
        return {"crew_config": crew, "shift": 480, "days": 20, "safety": 1.0,
                "efficiency": 1.0, "new_hire_pct": 0.0,
                "new_hire_productivity": 1.0, "absenteeism": 0.0, "overtime": 0.0}

    def test_partial_week_uses_true_working_days(self):
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        # June 2026 week 5 = Jun 29–30 = exactly 2 working days.
        full = self._units_on(machine_df, acc_df,
                              [("2026-06-29", 10), ("2026-06-30", 10)])
        inputs = self._inputs()
        wu = app._compute_weekly_utilization(full, machine_df, acc_df, inputs)
        assert wu is not None and "Wk 5" in wu.columns
        # Recompute the same week at a flat 5 days: the fixed value must be
        # exactly 5/2 = 2.5× higher (2 real working days, not 5).
        units_w = app._expand_schedule(full, machine_df, acc_df)
        from core.labor_calculator import build_capacity_table
        cap5 = build_capacity_table(units_w, inputs["crew_config"], 480, 5, 1.0, 1.0)
        for station in ("PM Acc (Headunit)", "PDI"):
            fixed = float(wu.loc[station, "Wk 5"])
            flat5 = float(cap5.loc[station, "labor_util_safe"])
            assert flat5 > 0
            assert abs((fixed / flat5) - 2.5) < 0.02

    def test_full_week_unchanged(self):
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        # June 2026 week 2 = Jun 8–12 = a full 5 working days → no change vs flat 5.
        full = self._units_on(machine_df, acc_df, [("2026-06-08", 10)])
        inputs = self._inputs()
        wu = app._compute_weekly_utilization(full, machine_df, acc_df, inputs)
        units_w = app._expand_schedule(full, machine_df, acc_df)
        from core.labor_calculator import build_capacity_table
        cap5 = build_capacity_table(units_w, inputs["crew_config"], 480, 5, 1.0, 1.0)
        fixed = float(wu.loc["PDI", "Wk 2"])
        flat5 = float(cap5.loc["PDI", "labor_util_safe"])
        assert abs(fixed - flat5) < 1e-6

    def test_multi_month_uses_each_weeks_own_month(self):
        # Review regression guard: the working-day count must come from the
        # month a week's rows actually fall in, not a single schedule-wide
        # dominant month. May 2026 dominates the row count (mode month = May),
        # but June's week 1 (Jun 1–5) is a genuinely FULL 5-working-day week and
        # must be divided by 5 — not by May's week 1 (May 1 only = 1 day), which
        # would read 5× too high and paint a fine week as overloaded.
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        # Jun 1 → WEEK_OF_MONTH 1 (June); May 11 → WEEK_OF_MONTH 3 (May), so the
        # two months land in different buckets. May has more rows → mode = May.
        full = self._units_on(machine_df, acc_df,
                              [("2026-06-01", 10), ("2026-05-11", 40)])
        inputs = self._inputs()
        wu = app._compute_weekly_utilization(full, machine_df, acc_df, inputs)
        # June's week-1 rows only; its true capacity is the full 5-day week.
        june_wk1 = full[full["WEEK_OF_MONTH"] == 1]
        units_w = app._expand_schedule(june_wk1, machine_df, acc_df)
        from core.labor_calculator import build_capacity_table
        cap5 = build_capacity_table(units_w, inputs["crew_config"], 480, 5, 1.0, 1.0)
        for station in ("PM Acc (Headunit)", "PDI"):
            fixed = float(wu.loc[station, "Wk 1"])
            correct = float(cap5.loc[station, "labor_util_safe"])
            assert correct > 0
            assert abs(fixed - correct) < 1e-6
