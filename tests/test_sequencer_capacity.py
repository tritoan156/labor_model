"""The monthly level-loader must use the Capacity tab's resource budgets.

No precedence/material scheduling: rows remain intact and impossible plans
still get dates, but every overloaded week is diagnosed rather than called fit.
"""
import pandas as pd
import pytest

import app
from core.constants import STATION_KEY_TO_DISPLAY, week_of_month
from core.labor_calculator import build_capacity_table, expand_schedule


def catalogs(final=100, battery=0):
    machine = pd.DataFrame.from_dict({
        "BOSS70-001": {"Bat": battery, "FN_Assy": final, "Warehouse": 0,
                       "Wire": 0, "Trailer": 0, "PDI": 0, "QC": 0,
                       "Ship": 0, "ETO": 0},
        "BOSS70HS-001": {"Bat": 0, "FN_Assy": final, "Warehouse": 0,
                         "Wire": 0, "Trailer": 0, "PDI": 0, "QC": 0,
                         "Ship": 0, "ETO": 0},
    }, orient="index")
    return machine, pd.DataFrame()


def schedule(n=4, qty=1, sku="BOSS70-001"):
    return pd.DataFrame([{
        "FG_BASE": sku, "FG_RAW": sku, "FG SKU ID": sku, "ACC": "",
        "BUILD QTY": qty, "CARRYOVER": False, "PRODUCTION MONTH": "Feb-27",
        "PRODUCTION DAY": pd.NaT, "Customer note": f"keep {i}",
    } for i in range(n)])


def settings(**overrides):
    inputs = {
        "plan_month": (2027, 2), "shift": 480, "days": 20,
        "efficiency": 1.0, "safety": 1.0,
        "crew_config": pd.DataFrame({"HC": 10, "Conc": 10, "Crew": 1},
                                    index=list(STATION_KEY_TO_DISPLAY.values())),
    }
    inputs.update(overrides)
    return inputs


def capacity_for_week(df, machine, acc, inputs, week):
    subset = df[df["WEEK_OF_MONTH"] == week]
    units = expand_schedule(subset, machine, acc,
        support_minutes_per_unit=inputs.get("support_minutes_per_unit", 0),
        mh_folded_stations=app.mh_folded_station_keys(inputs.get("mh_folded_stations", ())))
    days = sum(week_of_month(d) == week
               for d in app._working_days_in_month(*inputs["plan_month"]))
    return build_capacity_table(units, inputs["crew_config"], inputs["shift"], days,
        inputs["safety"], inputs["efficiency"],
        **{k: inputs[k] for k in ("new_hire_pct", "new_hire_productivity",
                                  "absenteeism", "overtime") if k in inputs})


@pytest.mark.parametrize("kind", ["zero_hc", "all_zero", "missing_station", "zero_cells", "zero_crew"])
def test_no_capacity_never_passes_as_fit(kind):
    machine, acc = catalogs()
    inputs = settings()
    crew = inputs["crew_config"]
    if kind == "all_zero":
        crew["HC"] = 0
    elif kind == "missing_station":
        inputs["crew_config"] = crew.drop(index="Final Assembly")
    else:
        col = {"zero_hc": "HC", "zero_cells": "Conc", "zero_crew": "Crew"}[kind]
        crew.loc["Final Assembly", col] = 0
    original = schedule()
    before = original.copy(deep=True)
    proposed, overloads = app._suggest_schedule_dates(original, machine, acc, inputs)
    assert len(overloads) == len(original)
    assert all("Final" in o["stations"] for o in overloads)
    assert proposed["PRODUCTION DAY"].notna().all()
    pd.testing.assert_frame_equal(original, before)
    pd.testing.assert_series_equal(original["Customer note"], proposed["Customer note"])
    assert proposed["BUILD QTY"].sum() == original["BUILD QTY"].sum()
    diagnoses = app._diagnose_overloads(proposed, machine, acc, inputs, overloads)
    assert len(diagnoses) == 4
    assert all(d["extend_days"] is None for d in diagnoses)


def test_zero_capacity_at_unused_stations_is_not_an_overload():
    machine, acc = catalogs()
    inputs = settings()
    inputs["crew_config"]["HC"] = 0
    inputs["crew_config"]["Conc"] = 0
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc"]] = 1
    _, overloads = app._suggest_schedule_dates(schedule(), machine, acc, inputs)
    assert overloads == []


@pytest.mark.parametrize("factors", [
    {}, {"overtime": .25}, {"absenteeism": .5},
    {"new_hire_pct": .5, "new_hire_productivity": .4},
    {"efficiency": .65, "safety": .8, "overtime": .2,
     "absenteeism": .1, "new_hire_pct": .3, "new_hire_productivity": .7},
])
def test_suggestion_and_diagnosis_match_capacity_table(factors):
    machine, acc = catalogs(final=600)
    inputs = settings(plan_month=(2026, 8), **factors)
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc", "Crew"]] = [1, 2, 2]
    # Includes the one-day final week. The batch row remains indivisible.
    proposed, overloads = app._suggest_schedule_dates(schedule(13, qty=2), machine, acc, inputs)
    diagnoses = app._diagnose_overloads(proposed, machine, acc, inputs, overloads)
    diagnosed = {(d["week"], d["station_key"]) for d in diagnoses}
    for week in proposed["WEEK_OF_MONTH"].unique():
        cap = capacity_for_week(proposed, machine, acc, inputs, week).loc["Final Assembly"]
        expected_overload = max(cap.labor_util_safe, cap.thru_util_safe) > 1
        assert ((week, "Final") in diagnosed) == expected_overload
        if expected_overload:
            d = next(d for d in diagnoses if d["week"] == week)
            assert d["demand"] == pytest.approx(cap.labor_demand)
            assert d["capacity"] == pytest.approx(cap.labor_cap_safe)
            assert d["cell_demand"] / d["cell_capacity"] == pytest.approx(cap.thru_util_safe)
            assert d["hire_hc"] == max(0, cap.required_hc - cap.HC)
            assert d["freed_by_defer"] >= d["shortfall"]
    assert bool(overloads) == bool(diagnoses)


def test_overtime_can_make_a_row_fit_and_absence_can_make_it_fail():
    machine, acc = catalogs(final=2700)
    inputs = settings()
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc"]] = 1
    assert app._suggest_schedule_dates(schedule(1), machine, acc, inputs)[1]
    inputs["overtime"] = .25
    assert not app._suggest_schedule_dates(schedule(1), machine, acc, inputs)[1]
    inputs["absenteeism"] = .2
    assert app._suggest_schedule_dates(schedule(1), machine, acc, inputs)[1]


def test_battery_uses_fixed_pack_cycle_not_crew_or_finished_unit_count():
    machine, acc = catalogs(final=0, battery=3)
    inputs = settings()
    inputs["crew_config"].loc["Battery Assembly", ["HC", "Conc", "Crew"]] = [100, 1, 100]
    proposed, overloads = app._suggest_schedule_dates(schedule(1, qty=5), machine, acc, inputs)
    assert overloads[0]["stations"] == ["Battery"]  # 15 packs * 170 > 2400 cell-min
    d = app._diagnose_overloads(proposed, machine, acc, inputs, overloads)[0]
    assert d["cell_demand"] == 15 * 170
    assert d["hire_hc"] == 0 and d["add_cells"] == 1
    assert d["extend_days"] == 1
    assert sum(r["qty"] for r in d["defer_units"]) == 5
    cap = capacity_for_week(proposed, machine, acc, inputs, d["week"]).loc["Battery Assembly"]
    assert d["cell_demand"] / d["cell_capacity"] == pytest.approx(cap.thru_util_safe)


def test_material_handling_folding_and_labor_only_capacity():
    machine, acc = catalogs(final=0)
    machine["Warehouse"] = 1200
    machine["Ship"] = 1000
    inputs = settings(support_minutes_per_unit=300,
                      mh_folded_stations=["Warehouse (Pick)", "Ship"])
    inputs["crew_config"].loc[["Warehouse (Pick)", "Ship"], "HC"] = 0
    inputs["crew_config"].loc["Material Handling", ["HC", "Conc"]] = [1, 0]
    proposed, overloads = app._suggest_schedule_dates(schedule(1), machine, acc, inputs)
    assert overloads[0]["stations"] == ["MatHandling"]
    d = app._diagnose_overloads(proposed, machine, acc, inputs, overloads)[0]
    assert d["demand"] == 2500 and d["shortfall"] == 100
    assert d["add_cells"] == 0
    inputs["crew_config"].loc["Material Handling", "HC"] = 2
    assert not app._suggest_schedule_dates(schedule(1), machine, acc, inputs)[1]


def test_hs_final_cycle_override_is_preserved_in_mixed_row_profiles():
    machine, acc = catalogs(final=2000)
    inputs = settings()
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc", "Crew"]] = [10, 1, 4]
    df = pd.concat([schedule(4), schedule(4, sku="BOSS70HS-001")], ignore_index=True)
    proposed, overloads = app._suggest_schedule_dates(df, machine, acc, inputs)
    assert overloads  # 4 * 2000 + 4 * 500 > 4 * 2400
    for d in app._diagnose_overloads(proposed, machine, acc, inputs, overloads):
        cap = capacity_for_week(proposed, machine, acc, inputs, d["week"]).loc["Final Assembly"]
        assert d["cell_demand"] / d["cell_capacity"] == pytest.approx(cap.thru_util_safe)


def test_weekly_diagnosis_does_not_disappear_into_monthly_spare_capacity():
    machine, acc = catalogs(final=2500)
    inputs = settings(days=1)  # sidebar may currently show This week / Remaining
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc"]] = 1
    proposed, overloads = app._suggest_schedule_dates(schedule(1), machine, acc, inputs)
    d = app._diagnose_overloads(proposed, machine, acc, inputs, overloads)[0]
    assert d["capacity"] == 2400 and d["shortfall"] == 100
    inputs["days"] = 20
    assert app._diagnose_overloads(proposed, machine, acc, inputs, overloads)[0] == d


def test_exact_capacity_is_fit_and_unknown_fg_behavior_is_preserved():
    machine, acc = catalogs(final=2400)
    inputs = settings()
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc"]] = 1
    proposed, overloads = app._suggest_schedule_dates(schedule(4), machine, acc, inputs)
    assert not overloads
    assert sorted(proposed["WEEK_OF_MONTH"]) == [1, 2, 3, 4]
    unknown = schedule(1, sku="UNKNOWN")
    proposed, overloads = app._suggest_schedule_dates(unknown, machine, acc, inputs)
    assert len(proposed) == 1 and not overloads  # guide retains catalog-gap warning


def test_facility_rerouting_removes_demand_from_absent_team():
    machine, acc = catalogs(final=100)
    machine["Final Station"] = "ComAcc"
    inputs = settings()
    inputs["crew_config"].loc["Final Assembly", ["HC", "Conc"]] = 0
    assert not app._suggest_schedule_dates(schedule(), machine, acc, inputs)[1]


def test_proposal_ui_explains_zero_capacity_without_suggesting_zero_extra_days():
    from streamlit.testing.v1 import AppTest
    machine, acc = catalogs()
    inputs = settings()
    inputs["crew_config"].loc["Final Assembly", "HC"] = 0
    proposed, overloads = app._suggest_schedule_dates(schedule(1), machine, acc, inputs)
    at = AppTest.from_string('''
import streamlit as st
from app import _render_sequencer_controls
_render_sequencer_controls(st.session_state["_proposed_schedule"],
    st.session_state["machine"], st.session_state["acc"], st.session_state["inputs"])
''')
    for key, value in {"machine": machine, "acc": acc, "inputs": inputs,
                       "_proposed_schedule": proposed,
                       "_proposed_schedule_overloads": overloads}.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception
    assert any("couldn't fit" in w.value for w in at.warning)
    assert any("Extra days cannot resolve this" in m.value for m in at.markdown)
    assert any("+1 people" in c.value for c in at.caption)
    assert any("Week 1" in m.value for m in at.markdown)
