"""Tests for core.labor_calculator — the per-unit labor and capacity math.

Expected values are hand-computed from the documented business rules so the
suite pins the calculations (and the audit fixes) against regression.
"""
import numpy as np
import pandas as pd
import pytest

from core.labor_calculator import (
    classify_unit, classify_fg_category, compute_unit_labor, unit_labor_split,
    expand_schedule, station_demand_table, cycle_for_unit, weighted_avg_cycle,
    build_capacity_table, buildable_by_line, _status_emoji, _overall_status,
    battery_demand_by_sku, battery_demand_by_type,
    labor_availability_factor, adjusted_efficiency, executive_summary,
)
from core.constants import STATION_KEYS, DEFAULT_BATT_RAW, DEFAULT_NAMEPLATE_PREP


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------
MACHINE_COLS = ["Bat", "Warehouse", "Wire", "Trailer", "QC", "Ship",
                "FN_Assy", "PDI", "ETO",
                "Final Station", "AccKIT Station", "PDI Station", "Wire Station"]
ACC_COLS = ["BattSubRaw", "Nameplate Prep", "Warehouse",
            "PMAcc", "GenAcc", "ComAcc", "AccKIT"]


def make_machine_df(rows, include_eto=True):
    """rows: {fg_base: {column: value}}. Missing columns default to 0 /
    default routing. Pass include_eto=False to omit the ETO column entirely
    (exercises the legacy fallback)."""
    cols = [c for c in MACHINE_COLS if include_eto or c != "ETO"]
    data = {}
    for fg, ov in rows.items():
        r = {c: 0 for c in cols}
        r["Final Station"] = "Final"
        r["AccKIT Station"] = "AccKIT"
        r["PDI Station"] = "PDI"
        r["Wire Station"] = "Wire"
        r.update(ov)
        data[fg] = r
    return pd.DataFrame.from_dict(data, orient="index")


def make_acc_df(rows):
    data = {}
    for sku, ov in rows.items():
        r = {c: 0 for c in ACC_COLS}
        r.update(ov)
        data[sku] = r
    return pd.DataFrame.from_dict(data, orient="index")


def make_units_df(units):
    """units: list of dicts; missing station columns default to 0."""
    rows = []
    for i, u in enumerate(units, start=1):
        r = {k: 0 for k in STATION_KEYS}
        r.update({"Class": "STD", "Bat": 0, "unit_id": i,
                  "fg_base": "FG", "batt_type": "25-12"})
        r.update(u)
        rows.append(r)
    return pd.DataFrame(rows)


def make_crew(d):
    """d: {station_display: (HC, Conc, Crew)}."""
    return pd.DataFrame(
        [{"HC": hc, "Conc": c, "Crew": cr} for (hc, c, cr) in d.values()],
        index=list(d.keys()),
    )


# --------------------------------------------------------------------------
# classify_unit
# --------------------------------------------------------------------------
class TestClassifyUnit:
    def test_hs(self):
        assert classify_unit("BOSS70HS-001") == "HS"

    def test_ht(self):
        assert classify_unit("BOSSHT-200") == "HT"

    def test_std(self):
        assert classify_unit("BOSS125-004") == "STD"

    def test_hs_precedence_over_ht(self):
        # "HS" is checked before "HT".
        assert classify_unit("BOSSHSHT") == "HS"


# --------------------------------------------------------------------------
# compute_unit_labor / unit_labor_split
# --------------------------------------------------------------------------
class TestComputeUnitLabor:
    def test_multi_battery_machine_default(self):
        # BOSS220HS-002, Bat=3, acc supplies no battery → machine default:
        # batt = DEFAULT_BATT_RAW*3 + DEFAULT_NAMEPLATE_PREP = 320*3+10 = 970.
        m = make_machine_df({"BOSS220HS-002": {
            "Bat": 3, "Warehouse": 5, "Wire": 10, "Trailer": 20,
            "QC": 8, "Ship": 4, "FN_Assy": 45, "PDI": 12, "ETO": 0}})
        a = make_acc_df({"ACC1": {"Warehouse": 2, "PMAcc": 15, "AccKIT": 7}})
        r = compute_unit_labor("BOSS220HS-002", "ACC1", m, a)
        assert r["Battery"] == DEFAULT_BATT_RAW * 3 + DEFAULT_NAMEPLATE_PREP == 970
        assert r["Warehouse"] == 7      # 5 (machine) + 2 (acc)
        assert r["Final"] == 45
        assert r["AccKIT"] == 7
        assert r["PDI"] == 12
        total = sum(r.get(s, 0) for s in STATION_KEYS)
        assert total == 1098

    def test_acc_supplied_battery(self):
        # acc supplies BattSubRaw/Nameplate → batt = btr*bat + nameplate.
        m = make_machine_df({"BOSS25-006": {"Bat": 1, "FN_Assy": 20}})
        a = make_acc_df({"ACC1": {"BattSubRaw": 300, "Nameplate Prep": 10}})
        r = compute_unit_labor("BOSS25-006", "ACC1", m, a)
        assert r["Battery"] == 300 * 1 + 10 == 310

    def test_non_boss_has_no_battery(self):
        # PDS/SDG may carry Bat=1 in the catalog but have no real batteries.
        m = make_machine_df({"PDS-100": {"Bat": 1, "FN_Assy": 30}})
        r = compute_unit_labor("PDS-100", None, m, make_acc_df({}))
        assert r["Bat"] == 0
        assert r["Battery"] == 0

    def test_eto_from_catalog(self):
        m = make_machine_df({"BOSS400-001": {"ETO": 960, "FN_Assy": 0}})
        r = compute_unit_labor("BOSS400-001", None, m, make_acc_df({}))
        assert r["ETO"] == 960

    def test_eto_legacy_fallback_when_column_missing(self):
        m = make_machine_df({"BOSS220X": {"FN_Assy": 0}}, include_eto=False)
        r = compute_unit_labor("BOSS220X", None, m, make_acc_df({}))
        assert r["ETO"] == 960  # BOSS220 → legacy 960
        m2 = make_machine_df({"BOSS70-001": {"FN_Assy": 0}}, include_eto=False)
        r2 = compute_unit_labor("BOSS70-001", None, m2, make_acc_df({}))
        assert r2["ETO"] == 0

    def test_routing_final_to_comacc(self):
        # FN_Assy routed to ComAcc lands at ComAcc, not Final; total unchanged.
        m = make_machine_df({"PDS-9": {"FN_Assy": 45, "Final Station": "ComAcc"}})
        r = compute_unit_labor("PDS-9", None, m, make_acc_df({}))
        assert r["Final"] == 0
        assert r["ComAcc"] == 45

    def test_routing_wire_default_to_wire(self):
        # No Wire Station override → wire labor stays at the Wire station.
        m = make_machine_df({"SDG-1": {"Wire": 30}})
        r = compute_unit_labor("SDG-1", None, m, make_acc_df({}))
        assert r["Wire"] == 30

    def test_routing_wire_reroute(self):
        # A plant with no Wire team folds wire labor into the chosen station
        # (e.g. Cypress: SDG → GenAcc), leaving Wire at 0 and the total intact.
        m = make_machine_df({"SDG-1": {"Wire": 30, "Wire Station": "GenAcc"}})
        r = compute_unit_labor("SDG-1", None, m, make_acc_df({}))
        assert r["Wire"] == 0
        assert r["GenAcc"] == 30
        # PDS → ComAcc variant
        m2 = make_machine_df({"PDS-2": {"Wire": 20, "Wire Station": "ComAcc"}})
        r2 = compute_unit_labor("PDS-2", None, m2, make_acc_df({}))
        assert r2["Wire"] == 0 and r2["ComAcc"] == 20

    def test_missing_fg_returns_none(self):
        assert compute_unit_labor("NOPE", None, make_machine_df({"X": {}}),
                                  make_acc_df({})) is None

    def test_nan_machine_cell_does_not_poison_total(self):
        # Audit fix: a NaN machine cell must be treated as 0, not propagate NaN
        # into total_labor, and the machine/acc split must still equal the total.
        m = make_machine_df({"BOSS125-001": {"Bat": 0, "Wire": np.nan,
                                             "FN_Assy": 30, "QC": 5}})
        r = compute_unit_labor("BOSS125-001", None, m, make_acc_df({}))
        total = sum(r.get(s, 0) for s in STATION_KEYS)
        assert not np.isnan(total)
        assert r["Wire"] == 0.0

    def test_bat_nan_does_not_crash(self):
        # Audit fix: int(m["Bat"]) on a NaN previously raised ValueError.
        m = make_machine_df({"BOSS25-006": {"Bat": np.nan, "FN_Assy": 10}})
        r = compute_unit_labor("BOSS25-006", None, m, make_acc_df({}))
        assert r is not None
        assert r["Bat"] == 0


class TestUnitLaborSplit:
    @pytest.mark.parametrize("fg,mrow,acc,arow", [
        ("BOSS220HS-002", {"Bat": 3, "Warehouse": 5, "Wire": 10, "Trailer": 20,
                           "QC": 8, "Ship": 4, "FN_Assy": 45, "PDI": 12, "ETO": 0},
         "ACC1", {"Warehouse": 2, "PMAcc": 15, "AccKIT": 7}),
        ("BOSS25-006", {"Bat": 1, "FN_Assy": 20},
         "ACC2", {"BattSubRaw": 300, "Nameplate Prep": 10, "GenAcc": 5}),
        ("PDS-9", {"FN_Assy": 45, "Final Station": "ComAcc", "QC": 3}, None, {}),
    ])
    def test_split_sums_to_total(self, fg, mrow, acc, arow):
        m = make_machine_df({fg: mrow})
        a = make_acc_df({acc: arow}) if acc else make_acc_df({})
        full = compute_unit_labor(fg, acc, m, a)
        total = sum(full.get(s, 0) for s in STATION_KEYS)
        split = unit_labor_split(fg, acc, m, a)
        assert split["machine"] + split["acc"] == pytest.approx(total)

    def test_split_nan_cell_still_matches_total(self):
        m = make_machine_df({"BOSS125-001": {"Bat": 0, "Wire": np.nan,
                                             "FN_Assy": 30, "QC": 5}})
        full = compute_unit_labor("BOSS125-001", None, m, make_acc_df({}))
        total = sum(full.get(s, 0) for s in STATION_KEYS)
        split = unit_labor_split("BOSS125-001", None, m, make_acc_df({}))
        assert split["machine"] + split["acc"] == pytest.approx(total)


# --------------------------------------------------------------------------
# expand_schedule / station_demand_table
# --------------------------------------------------------------------------
class TestExpandSchedule:
    def _schedule(self):
        return pd.DataFrame([
            {"BUILD QTY": 2, "FG_BASE": "BOSS25-006", "FG_RAW": "BOSS25-006",
             "ACC": "ACC1", "DECAL": "", "CUSTOMER NAME": "X", "CARRYOVER": False},
            {"BUILD QTY": 1, "FG_BASE": "BOSS220HS-002", "FG_RAW": "BOSS220HS-002",
             "ACC": "", "DECAL": "", "CUSTOMER NAME": "Y", "CARRYOVER": False},
            {"BUILD QTY": 5, "FG_BASE": "NOPE", "FG_RAW": "NOPE",
             "ACC": "", "DECAL": "", "CUSTOMER NAME": "Z", "CARRYOVER": False},
            {"BUILD QTY": 1, "FG_BASE": "BOSS25-006", "FG_RAW": "BOSS25-006",
             "ACC": "ACCX", "DECAL": "", "CUSTOMER NAME": "W", "CARRYOVER": True},
        ])

    def _catalog(self):
        m = make_machine_df({
            "BOSS25-006": {"Bat": 1, "FN_Assy": 20, "QC": 5},
            "BOSS220HS-002": {"Bat": 3, "FN_Assy": 45},
        })
        a = make_acc_df({"ACC1": {"PMAcc": 10}})
        return m, a

    def test_qty_expansion_and_skips(self):
        m, a = self._catalog()
        units = expand_schedule(self._schedule(), m, a)
        # 2 + 1 + (NOPE skipped) + 1 = 4 units
        assert len(units) == 4
        assert units.attrs["skipped_fg"] == ["NOPE"]
        assert "ACCX" in units.attrs["unknown_acc"]
        assert (units["total_labor"] > 0).all()

    def test_station_demand_table(self):
        m, a = self._catalog()
        units = expand_schedule(self._schedule(), m, a)
        demand = station_demand_table(units)
        # QC labor = 5 per BOSS25-006 unit × 3 such units = 15.
        assert demand.loc["QC", "demand"] == pytest.approx(15.0)
        assert demand.loc["QC", "units_touching"] == 3


# --------------------------------------------------------------------------
# cycle_for_unit / weighted_avg_cycle
# --------------------------------------------------------------------------
class TestCycleForUnit:
    def test_standard(self):
        assert cycle_for_unit("QC", 100, "STD", 4) == 25

    def test_final_hs_uses_crew_one(self):
        # HS Final uses HS_FINAL_CREW=1, not the station crew.
        assert cycle_for_unit("Final", 30, "HS", 2) == 30

    def test_zero_labor(self):
        assert cycle_for_unit("QC", 0, "STD", 4) == 0

    def test_crew_zero_no_crash(self):
        # Audit fix: crew=0 returned a ZeroDivisionError; now returns 0.0.
        assert cycle_for_unit("QC", 100, "STD", 0) == 0.0


class TestWeightedAvgCycle:
    def test_volume_weighted_mean(self):
        # Final: two STD (60/2=30) and one HS (30/1=30) → mean 30.
        units = make_units_df([
            {"Final": 60, "Class": "STD"},
            {"Final": 60, "Class": "STD"},
            {"Final": 30, "Class": "HS"},
        ])
        assert weighted_avg_cycle(units, "Final", crew=2) == pytest.approx(30.0)

    def test_reconstructs_total_cell_time(self):
        # units_touching × avg_cycle must equal the sum of per-unit cycles
        # (the identity that keeps thru_util exact).
        units = make_units_df([
            {"QC": 100, "Class": "STD"},
            {"QC": 40, "Class": "STD"},
        ])
        avg = weighted_avg_cycle(units, "QC", crew=2)
        touching = int((units["QC"] > 0).sum())
        assert touching * avg == pytest.approx((100 / 2) + (40 / 2))


# --------------------------------------------------------------------------
# build_capacity_table
# --------------------------------------------------------------------------
class TestBuildCapacityTable:
    def test_labor_and_throughput_qc(self):
        units = make_units_df([{"QC": 100}, {"QC": 100}])
        crew = make_crew({"QC": (4, 4, 1)})
        cap = build_capacity_table(units, crew, shift_minutes=480,
                                   working_days=20, safety_factor=1.0,
                                   efficiency_factor=0.5)
        row = cap.loc["QC"]
        assert row["labor_demand"] == pytest.approx(200.0)
        assert row["labor_cap_safe"] == pytest.approx(4 * 480 * 20 * 0.5)  # 19200
        assert row["labor_util_safe"] == pytest.approx(200 / 19200)
        assert row["required_hc"] == 1          # ceil(200 / 4800)
        assert row["avg_cycle"] == pytest.approx(100.0)
        assert row["need_per_day"] == pytest.approx(2 / 20)
        assert row["thru_cap_safe"] == pytest.approx(4 * (480 / 100) * 0.5)  # 9.6
        assert row["overall_status"].startswith("🟢")

    def test_battery_branch_counts_batteries(self):
        units = make_units_df([
            {"Battery": 970, "Bat": 3, "fg_base": "BOSS220HS-002"},
        ])
        crew = make_crew({"Battery Assembly": (8, 4, 2)})
        cap = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)
        row = cap.loc["Battery Assembly"]
        assert row["units_or_batt"] == 3          # batteries, not units
        assert row["avg_cycle"] == 170            # BATTERY_CYCLE_MINUTES

    def test_conc_zero_with_demand_flags_red(self):
        # Audit fix: HC>0 but Conc=0 (no bays) + demand → 🔴, not false 🟢.
        units = make_units_df([{"QC": 100}, {"QC": 100}])
        crew = make_crew({"QC": (4, 0, 1)})
        cap = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)
        row = cap.loc["QC"]
        assert row["thru_status_safe"] == "🔴"
        assert row["overall_status"].startswith("🔴")

    def test_crew_zero_no_crash(self):
        # Audit fix: Crew=0 previously crashed the whole table.
        units = make_units_df([{"QC": 100}, {"QC": 100}])
        crew = make_crew({"QC": (4, 4, 0)})
        cap = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)  # no raise
        assert cap.loc["QC"]["overall_status"].startswith("🔴")

    def test_zero_working_days_guarded(self):
        units = make_units_df([{"QC": 100}])
        crew = make_crew({"QC": (4, 4, 1)})
        cap = build_capacity_table(units, crew, 480, 0, 1.0, 0.5)  # no raise
        assert cap.loc["QC"]["required_hc"] == 0

    def test_workforce_factor_hits_labor_not_throughput(self):
        # New-hire ramp + absenteeism reduce LABOR capacity / raise Required-HC,
        # but must leave physical throughput (thru_cap_safe / thru_util_safe)
        # byte-for-byte identical — Space-limits is judged on cells, not staffing.
        units = make_units_df([{"QC": 100}, {"QC": 100}])
        crew = make_crew({"QC": (4, 4, 1)})
        base = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)
        adj = build_capacity_table(units, crew, 480, 20, 1.0, 0.5,
                                   new_hire_pct=0.2, new_hire_productivity=0.5,
                                   absenteeism=0.05)
        f = labor_availability_factor(0.2, 0.5, 0.05)  # 0.9 * 0.95 = 0.855
        # Throughput side: unchanged
        assert adj.loc["QC"]["thru_cap_safe"] == pytest.approx(base.loc["QC"]["thru_cap_safe"])
        assert adj.loc["QC"]["thru_util_safe"] == pytest.approx(base.loc["QC"]["thru_util_safe"])
        # Labor side: scaled by the factor
        assert adj.loc["QC"]["labor_cap_safe"] == pytest.approx(base.loc["QC"]["labor_cap_safe"] * f)
        assert adj.loc["QC"]["labor_util_safe"] == pytest.approx(base.loc["QC"]["labor_util_safe"] / f)

    def test_workforce_factor_defaults_are_noop(self):
        units = make_units_df([{"QC": 100}, {"QC": 100}])
        crew = make_crew({"QC": (4, 4, 1)})
        base = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)
        same = build_capacity_table(units, crew, 480, 20, 1.0, 0.5,
                                    new_hire_pct=0.0, new_hire_productivity=1.0,
                                    absenteeism=0.0)
        assert same.loc["QC"]["labor_cap_safe"] == pytest.approx(base.loc["QC"]["labor_cap_safe"])

    def test_overtime_lifts_labor_and_throughput(self):
        # Overtime extends working time, so BOTH labor and throughput capacity
        # scale by (1+overtime) — and required_hc drops. Contrast the labor-only
        # workforce factor above (which leaves thru_cap_safe untouched).
        units = make_units_df([{"QC": 100} for _ in range(40)])
        crew = make_crew({"QC": (4, 4, 1)})
        base = build_capacity_table(units, crew, 480, 20, 1.0, 0.5)
        ot = build_capacity_table(units, crew, 480, 20, 1.0, 0.5, overtime=0.25)
        assert ot.loc["QC"]["labor_cap_safe"] == pytest.approx(base.loc["QC"]["labor_cap_safe"] * 1.25)
        assert ot.loc["QC"]["thru_cap_safe"] == pytest.approx(base.loc["QC"]["thru_cap_safe"] * 1.25)
        assert ot.loc["QC"]["required_hc"] <= base.loc["QC"]["required_hc"]


# --------------------------------------------------------------------------
# status emojis
# --------------------------------------------------------------------------
class TestStatusEmoji:
    def test_thresholds(self):
        assert _status_emoji(1.01) == "🔴"   # > OVER (1.00)
        assert _status_emoji(1.00) == "🟠"   # exactly 1.00 is NOT over
        assert _status_emoji(0.95) == "🟠"   # > NEAR (0.90)
        assert _status_emoji(0.90) == "🟡"   # exactly 0.90 → tight
        assert _status_emoji(0.80) == "🟡"   # > TIGHT (0.75)
        assert _status_emoji(0.75) == "🟢"   # exactly 0.75 → ok
        assert _status_emoji(0.10) == "🟢"

    def test_missing_station(self):
        assert _status_emoji(0.0, station_missing=True, has_demand=False) == "⚪"
        assert _status_emoji(0.0, station_missing=True, has_demand=True) == "🔴"


class TestOverallStatus:
    def _row(self, l_s, l_r, t_s, t_r, missing=False, demand=100):
        return pd.Series({
            "labor_status_safe": l_s, "labor_status_raw": l_r,
            "thru_status_safe": t_s, "thru_status_raw": t_r,
            "station_missing": missing, "labor_demand": demand,
        })

    def test_worst_of_four(self):
        assert _overall_status(self._row("🟢", "🟢", "🟠", "🟢")) == "🟠 NEAR-CAP"
        assert _overall_status(self._row("🟢", "🔴", "🟢", "🟢")) == "🔴 OVER"
        assert _overall_status(self._row("🟢", "🟢", "🟢", "🟢")) == "🟢 OK"
        assert _overall_status(self._row("🟡", "🟢", "🟢", "🟢")) == "🟡 TIGHT"

    def test_missing_precedence(self):
        assert _overall_status(self._row("🔴", "🔴", "🔴", "🔴",
                                         missing=True, demand=100)) == "🔴 NO STATION"
        assert _overall_status(self._row("⚪", "⚪", "⚪", "⚪",
                                         missing=True, demand=0)) == "⚪ N/A"


# --------------------------------------------------------------------------
# battery demand
# --------------------------------------------------------------------------
class TestBatteryDemand:
    def _units(self):
        return make_units_df([
            {"Battery": 970, "Bat": 3, "fg_base": "BOSS220HS-002", "batt_type": "125-20"},
            {"Battery": 970, "Bat": 3, "fg_base": "BOSS220HS-002", "batt_type": "125-20"},
            {"Battery": 330, "Bat": 1, "fg_base": "BOSS25-006", "batt_type": "25-12"},
        ])

    def test_by_sku(self):
        g = battery_demand_by_sku(self._units()).set_index("fg_base")
        assert g.loc["BOSS220HS-002", "total_batteries"] == 6   # 2 units × 3
        assert g.loc["BOSS25-006", "total_batteries"] == 1
        assert g["pct_of_total"].sum() == pytest.approx(1.0)

    def test_by_type(self):
        g = battery_demand_by_type(self._units()).set_index("batt_type")
        assert g.loc["125-20", "total_batteries"] == 6
        assert g.loc["25-12", "total_batteries"] == 1


# --------------------------------------------------------------------------
# classify_fg_category / buildable_by_line (line-aware buildable units)
# --------------------------------------------------------------------------
class TestClassifyFgCategory:
    def test_compressor(self):
        assert classify_fg_category("PDS100-001") == "Compressor"

    def test_generator(self):
        assert classify_fg_category("SDG125-006") == "Generator"

    def test_eboss_full_unit(self):
        assert classify_fg_category("BOSS125-001") == "EBOSS"

    def test_pm_headskid(self):
        assert classify_fg_category("BOSS125HS-001") == "PM"

    def test_pm_even_with_trailer_anomaly(self):
        # HS units are PM even if the catalog gives them a little trailer labor.
        assert classify_fg_category("BOSS70HS-001") == "PM"

    def test_other(self):
        assert classify_fg_category("") == "Other"
        assert classify_fg_category("WIDGET-1") == "Other"


class TestBuildableByLine:
    def _cap(self, rows):
        """rows: {station_display: (HC, labor_util_safe, thru_util_safe)}."""
        df = pd.DataFrame(
            [{"HC": hc, "labor_util_safe": lu, "thru_util_safe": tu}
             for (hc, lu, tu) in rows.values()],
            index=list(rows.keys()),
        )
        df.index.name = "station_display"
        return df

    def test_single_line_back_compat(self):
        # One line → equals floor(N / global max-util), like the old single cap.
        units = make_units_df([{"fg_base": "BOSS125-001", "GenAcc": 10, "Warehouse": 5}
                               for _ in range(100)])
        cap = self._cap({"Gen Accessories": (4, 0.5, 0.0),
                         "Warehouse (Pick)": (4, 0.1, 0.0)})
        r = buildable_by_line(units, cap)
        assert r["total"] == 200          # 100 / 0.5
        assert len(r["lines"]) == 1
        assert r["lines"][0]["binding_station"] == "Gen Accessories"

    def test_exclusive_lines_no_cross_throttle(self):
        # Compressor (PDS) uses Com Accessories (util 2.0); Generator (SDG) uses
        # Gen Accessories (util 0.5). The compressor cap must NOT be dragged down
        # by the generator-only station, and vice-versa.
        units = make_units_df(
            [{"fg_base": "PDS100-001", "ComAcc": 10, "Warehouse": 5} for _ in range(10)]
            + [{"fg_base": "SDG125-006", "GenAcc": 10, "Warehouse": 5} for _ in range(20)]
        )
        cap = self._cap({"Com Accessories": (4, 2.0, 0.0),
                         "Gen Accessories": (4, 0.5, 0.0),
                         "Warehouse (Pick)": (4, 0.1, 0.0)})
        r = buildable_by_line(units, cap)
        by = {row["line"]: row for row in r["lines"]}
        assert by["Compressor"]["buildable"] == 5     # 10 / 2.0
        assert by["Compressor"]["binding_station"] == "Com Accessories"
        assert by["Generator"]["buildable"] == 40     # 20 / 0.5 (uncapped)
        assert r["total"] == 45

    def test_shared_station_caps_both_and_stays_feasible(self):
        # Warehouse is shared (combined util 2.0) and binds both lines.
        units = make_units_df(
            [{"fg_base": "PDS100-001", "ComAcc": 10, "Warehouse": 5} for _ in range(10)]
            + [{"fg_base": "SDG125-006", "GenAcc": 10, "Warehouse": 5} for _ in range(10)]
        )
        cap = self._cap({"Com Accessories": (4, 0.5, 0.0),
                         "Gen Accessories": (4, 0.5, 0.0),
                         "Warehouse (Pick)": (4, 2.0, 0.0)})
        r = buildable_by_line(units, cap)
        by = {row["line"]: row for row in r["lines"]}
        assert by["Compressor"]["buildable"] == 5     # capped by Warehouse 2.0
        assert by["Generator"]["buildable"] == 5
        assert by["Compressor"]["binding_station"] == "Warehouse (Pick)"
        assert r["total"] == 10

    def test_throughput_can_bind(self):
        units = make_units_df([{"fg_base": "BOSS125-001", "Battery": 10} for _ in range(60)])
        cap = self._cap({"Battery Assembly": (8, 0.2, 1.5)})  # throughput-bound
        r = buildable_by_line(units, cap)
        assert r["lines"][0]["binding_kind"] == "throughput"
        assert r["lines"][0]["buildable"] == 40       # 60 / 1.5

    def test_unstaffed_station_makes_line_infeasible(self):
        units = make_units_df([{"fg_base": "PDS100-001", "ComAcc": 10} for _ in range(10)])
        cap = self._cap({"Com Accessories": (0, 0.0, 0.0)})  # HC=0
        r = buildable_by_line(units, cap)
        assert r["lines"][0]["infeasible"] is True
        assert r["lines"][0]["buildable"] == 0
        assert r["total"] == 0

    def test_empty(self):
        assert buildable_by_line(pd.DataFrame(), self._cap({"X": (1, 0.5, 0)})) == {"lines": [], "total": 0}


# --------------------------------------------------------------------------
# Workforce-availability factors
# --------------------------------------------------------------------------
class TestWorkforceFactors:
    def test_labor_availability_factor(self):
        # (1 - 0.10*(1-0.50)) * (1 - 0.05) = 0.95 * 0.95 = 0.9025
        assert labor_availability_factor(0.10, 0.50, 0.05) == pytest.approx(0.95 * 0.95)
        # No new hires, no absenteeism → 1.0 (no-op)
        assert labor_availability_factor(0.0, 1.0, 0.0) == pytest.approx(1.0)
        # All new hires at 0% productivity, 0 absent → 0.0
        assert labor_availability_factor(1.0, 0.0, 0.0) == pytest.approx(0.0)

    def test_labor_availability_factor_clamps(self):
        # Out-of-range / bad inputs clamp to [0,1] rather than blow up.
        # nh 2.0 → 1.0; prod 0.5; absent 0 → (1 - 1.0*0.5) = 0.5
        assert labor_availability_factor(2.0, 0.5, 0.0) == pytest.approx(0.5)
        # NaN new-hire share → treated as 0 → no drag → 1.0
        assert labor_availability_factor(float("nan"), 0.5, 0.0) == pytest.approx(1.0)

    def test_adjusted_efficiency_matches_legacy_sheet(self):
        # The three facility columns from the legacy planning spreadsheet.
        assert adjusted_efficiency(0.45, 0.10, 0.50) == pytest.approx(0.4275)
        assert adjusted_efficiency(0.45, 0.40, 0.50) == pytest.approx(0.36)
        assert adjusted_efficiency(0.50, 0.20, 0.50) == pytest.approx(0.45)

    def test_adjusted_efficiency_excludes_absenteeism(self):
        # Absenteeism is reported/applied separately, never folded into adj-eff.
        assert adjusted_efficiency(0.50, 0.0, 1.0) == pytest.approx(0.50)


# --------------------------------------------------------------------------
# executive_summary — reproduces the legacy monthly planning spreadsheet
# --------------------------------------------------------------------------
class TestExecutiveSummary:
    def _run(self, earned_hr, units, hc, base, nh, prod):
        return executive_summary(
            total_earned_minutes=earned_hr * 60, total_units=units,
            available_hc=hc, shift_minutes=480, working_days=20,
            base_efficiency=base, new_hire_pct=nh, new_hire_productivity=prod,
            absenteeism=0.05, safety=1.0,
        )

    def test_cypress_column(self):
        r = self._run(1784, 226, 23, 0.45, 0.10, 0.50)
        assert r["adjusted_efficiency"] == pytest.approx(0.4275)
        assert r["headcount_needed_aggregate"] == pytest.approx(27.46, abs=0.05)
        assert r["net_available_hours"] == pytest.approx(3680.0)        # 23*160
        assert r["available_earned_hours"] == pytest.approx(1494.54, abs=0.05)
        assert r["missed_units_aggregate"] == pytest.approx(36.7, abs=0.1)
        assert r["verdict"] == "No Good"

    def test_henderson_column(self):
        r = self._run(2927, 200, 42, 0.45, 0.40, 0.50)
        assert r["adjusted_efficiency"] == pytest.approx(0.36)
        assert r["headcount_needed_aggregate"] == pytest.approx(53.49, abs=0.05)
        assert r["available_earned_hours"] == pytest.approx(2298.24, abs=0.05)
        assert r["missed_units_aggregate"] == pytest.approx(43.0, abs=0.1)
        assert r["verdict"] == "No Good"

    def test_spartanburg_column(self):
        r = self._run(2505, 368, 36, 0.50, 0.20, 0.50)
        assert r["adjusted_efficiency"] == pytest.approx(0.45)
        assert r["headcount_needed_aggregate"] == pytest.approx(36.62, abs=0.05)
        assert r["available_earned_hours"] == pytest.approx(2462.4, abs=0.05)
        assert r["missed_units_aggregate"] == pytest.approx(6.3, abs=0.1)
        assert r["verdict"] == "No Good"

    def test_good_verdict_when_staffed(self):
        # Same Cypress demand but plenty of staff → Good, zero missed.
        r = self._run(1784, 226, 40, 0.45, 0.10, 0.50)
        assert r["verdict"] == "Good"
        assert r["missed_units_aggregate"] == pytest.approx(0.0)

    def test_station_aware_passthrough(self):
        r = executive_summary(
            total_earned_minutes=1784 * 60, total_units=226, available_hc=23,
            shift_minutes=480, working_days=20, base_efficiency=0.45,
            new_hire_pct=0.10, new_hire_productivity=0.50, absenteeism=0.05,
            safety=1.0, station_required_hc=31.0, station_buildable=180,
        )
        assert r["headcount_needed_station"] == pytest.approx(31.0)
        assert r["missed_units_station"] == 46          # 226 - 180

    def test_zero_demand_is_good(self):
        r = executive_summary(
            total_earned_minutes=0, total_units=0, available_hc=10,
            shift_minutes=480, working_days=20, base_efficiency=0.5,
        )
        assert r["verdict"] == "Good"
        assert r["missed_units_aggregate"] == pytest.approx(0.0)

    def test_support_hc_counts_in_totals_only(self):
        # Lead/Other add to total headcount + total available hours, but never
        # change the production verdict / earned hours / headcount-needed.
        base = self._run(1784, 226, 23, 0.45, 0.10, 0.50)
        withsupp = executive_summary(
            total_earned_minutes=1784 * 60, total_units=226, available_hc=23,
            shift_minutes=480, working_days=20, base_efficiency=0.45,
            new_hire_pct=0.10, new_hire_productivity=0.50, absenteeism=0.05,
            safety=1.0, support_hc=5,
        )
        assert withsupp["available_headcount"] == pytest.approx(23)      # production
        assert withsupp["support_headcount"] == pytest.approx(5)
        assert withsupp["total_headcount"] == pytest.approx(28)
        assert withsupp["total_available_hours"] == pytest.approx(28 * 160)
        # Production figures identical to the no-support run:
        for k in ("headcount_needed_aggregate", "available_earned_hours",
                  "net_available_hours", "missed_units_aggregate"):
            assert withsupp[k] == pytest.approx(base[k])
        assert withsupp["verdict"] == base["verdict"]

    def test_mh_per_unit_standard_and_loaded(self):
        # Spartanburg-like: 3,719 earned hrs / 419 units ≈ 8.875 mh/unit standard.
        r = self._run(3719, 419, 44, 0.50, 0.20, 0.75)
        assert r["mh_per_unit"] == pytest.approx(3719 / 419)
        # Loaded = standard ÷ (adj_eff × (1-absent)); adj_eff=0.475, absent=0.05.
        assert r["mh_per_unit_loaded"] == pytest.approx((3719 / 419) / (0.475 * 0.95))
        # Overtime must NOT change either mh/unit (it moves capacity, not content).
        ot = executive_summary(
            total_earned_minutes=3719 * 60, total_units=419, available_hc=44,
            shift_minutes=480, working_days=22, base_efficiency=0.50,
            new_hire_pct=0.20, new_hire_productivity=0.75, absenteeism=0.05,
            safety=1.0, overtime=0.10,
        )
        assert ot["mh_per_unit"] == pytest.approx(r["mh_per_unit"])
        assert ot["mh_per_unit_loaded"] == pytest.approx(r["mh_per_unit_loaded"])

    def test_total_available_hours_per_unit(self):
        # ALL staffed hours (production + support, gross incl. OT) ÷ units.
        r = executive_summary(
            total_earned_minutes=3719 * 60, total_units=419, available_hc=44,
            shift_minutes=480, working_days=22, base_efficiency=0.50,
            new_hire_pct=0.20, new_hire_productivity=0.75, absenteeism=0.05,
            safety=1.0, support_hc=6,
        )
        # (44+6) people × (480×22/60) hrs ÷ 419 units
        assert r["total_available_hours_per_unit"] == pytest.approx((50 * 176) / 419)
        assert r["total_available_hours_per_unit"] == pytest.approx(
            r["total_available_hours"] / 419)

    def test_overtime_scales_hours_and_lowers_hc_needed(self):
        base = self._run(1784, 226, 23, 0.45, 0.10, 0.50)
        ot = executive_summary(
            total_earned_minutes=1784 * 60, total_units=226, available_hc=23,
            shift_minutes=480, working_days=20, base_efficiency=0.45,
            new_hire_pct=0.10, new_hire_productivity=0.50, absenteeism=0.05,
            safety=1.0, overtime=0.25,
        )
        assert ot["overtime"] == pytest.approx(0.25)
        assert ot["effective_hours_per_person"] == pytest.approx(base["effective_hours_per_person"] * 1.25)
        assert ot["available_earned_hours"] == pytest.approx(base["available_earned_hours"] * 1.25)
        assert ot["headcount_needed_aggregate"] == pytest.approx(base["headcount_needed_aggregate"] / 1.25)
        assert ot["net_available_hours"] == pytest.approx(base["net_available_hours"] * 1.25)
