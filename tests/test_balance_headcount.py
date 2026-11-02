"""⚖️ Balance people — spread a fixed headcount over the stations for the most
output, chosen lines first (core.labor_calculator.balance_headcount and the
What-if button that runs it).

Henderson asked for it on 2026-11-02: "how many units can I do with 74 people
including material handlers", with the people placed where they produce the
most and Hybrid / Power Module / ETO built first.

Pinned here:

* a station gets ceil(minutes / minutes per person) people — the
  build_capacity_table rate — for the share of each line it carries;
* priority lines are covered before any other line gets a person;
* a line is held only by its own limits: one line's full cells don't hold
  back another line in the same group;
* full cells leave people over, and they're placed as buffer, never lost;
* Material Handling (labor-only) is never stopped by cells;
* the What-if button rewrites People to the plan (AppTest).
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from core.constants import STATION_DEFAULTS
from core.labor_calculator import (
    balance_headcount, build_capacity_table, expand_schedule,
)

SKUS = {
    # fg: (line, catalog minutes)
    "BOSS25-006": ("EBOSS", {"Bat": 1, "Warehouse": 10, "Trailer": 60, "FN_Assy": 40,
                             "QC": 20, "Ship": 10}),
    "BOSS70HS-001": ("PM", {"Bat": 1, "Warehouse": 10, "FN_Assy": 30, "QC": 20,
                            "ETO": 200}),
    "SDG25-001": ("Generator", {"Bat": 0, "Warehouse": 10, "Trailer": 40, "FN_Assy": 30,
                                "QC": 20}),
    "PDS185EZ-001": ("Compressor", {"Bat": 0, "Warehouse": 5, "FN_Assy": 60, "QC": 10}),
}
SETTINGS = dict(shift_minutes=480, safety_factor=1.0, efficiency_factor=0.5,
                new_hire_pct=0.2, new_hire_productivity=0.7, absenteeism=0.05)
PER_PERSON_18 = 480 * 18 * 0.5 * (1 - 0.2 * 0.3) * 0.95


def _catalog():
    cols = ["Bat", "Warehouse", "Wire", "Trailer", "QC", "Ship", "FN_Assy", "PDI", "ETO"]
    machine = pd.DataFrame.from_dict(
        {fg: {c: m.get(c, 0) for c in cols} for fg, (_l, m) in SKUS.items()},
        orient="index")
    for col, default in (("Final Station", "Final"), ("AccKIT Station", "AccKIT"),
                         ("PDI Station", "PDI"), ("Wire Station", "Wire"),
                         ("Undercarriage Station", "Undercarriage")):
        machine[col] = default
    acc = pd.DataFrame(columns=["BattSubRaw", "Nameplate Prep", "Warehouse",
                                "PMAcc", "GenAcc", "ComAcc", "AccKIT"])
    return machine, acc


def _units(qty=None, support=0.0):
    qty = qty or {"BOSS25-006": 600, "BOSS70HS-001": 200, "SDG25-001": 400, "PDS185EZ-001": 300}
    rows = [{"BUILD QTY": q, "FG_BASE": fg, "FG_RAW": fg, "ACC": "", "DECAL": "",
             "CUSTOMER NAME": "", "CARRYOVER": False} for fg, q in qty.items() if q]
    m, a = _catalog()
    return expand_schedule(pd.DataFrame(rows), m, a, support_minutes_per_unit=support)


def _crew(cells=None, mh=1):
    cells = cells or {}
    rows = {d: (1, cells.get(d, 50), 1) for d in STATION_DEFAULTS}
    rows["Material Handling"] = (mh, cells.get("Material Handling", 1), 1)
    return pd.DataFrame([{"HC": h, "Conc": c, "Crew": cr} for h, c, cr in rows.values()],
                        index=list(rows))


def _by_line(plan):
    return {r["line"]: r for r in plan["lines"]}


def _balance(units, crew, people, days=18, **kw):
    return balance_headcount(units, crew, people, working_days=days, **SETTINGS, **kw)


class TestPeopleAndOutput:
    def test_enough_people_cover_the_plan_and_the_rest_are_buffer(self):
        units = _units()
        plan = _balance(units, _crew(), 400)
        for r in plan["lines"]:
            assert r["units"] == r["planned"] and r["limit"] == ""
        assert sum(plan["hc"].values()) == 400
        assert plan["used"] + plan["spare"] == 400
        assert sum(plan["spare_at"].values()) == plan["spare"]

    def test_station_people_match_the_capacity_rate(self):
        units = _units()
        plan = _balance(units, _crew(), 400)
        cap = build_capacity_table(units, _crew(), 480, 18, 1.0, 0.5, new_hire_pct=0.2,
                                   new_hire_productivity=0.7, absenteeism=0.05)
        for station, need in cap["required_hc"].items():
            base = plan["hc"].get(station, 0) - plan["spare_at"].get(station, 0)
            assert base == need, station
        assert cap.loc["ETO", "required_hc"] == math.ceil(200 * 200 / PER_PERSON_18)

    def test_never_more_people_than_given(self):
        units = _units()
        for people in (0, 3, 30, 100):
            plan = _balance(units, _crew(), people)
            assert sum(plan["hc"].values()) <= people


class TestPriority:
    def test_other_lines_never_take_from_the_priority_lines(self):
        # Short of the whole plan, the priority lines get exactly what they'd
        # get if the other lines weren't in the plan at all.
        units = _units()
        people = _balance(units, _crew(), 400)["used"] - 25
        mixed = _by_line(_balance(units, _crew(), people))
        alone = _by_line(_balance(
            _units({"BOSS25-006": 600, "BOSS70HS-001": 200}), _crew(), people))
        for ln in ("EBOSS", "PM"):
            assert mixed[ln]["priority"]
            assert mixed[ln]["units"] == alone[ln]["units"], ln
        assert not mixed["Generator"]["priority"] and not mixed["Compressor"]["priority"]

    def test_priority_beats_no_priority_on_the_priority_lines(self):
        units = _units()
        people = _balance(units, _crew(), 400)["used"] // 2
        first = _by_line(_balance(units, _crew(), people))
        flat = _by_line(_balance(units, _crew(), people,
                                 priority_lines=("EBOSS", "PM", "Generator", "Compressor")))
        assert first["EBOSS"]["units"] + first["PM"]["units"] >= \
            flat["EBOSS"]["units"] + flat["PM"]["units"]

    def test_no_priority_lifts_the_weakest_line(self):
        # Raising every line together maximizes the lowest share.
        units = _units()
        people = _balance(units, _crew(), 400)["used"] // 2
        first = _balance(units, _crew(), people)
        flat = _balance(units, _crew(), people, priority_lines=())
        assert min(r["share"] for r in flat["lines"]) >=             min(r["share"] for r in first["lines"]) - 1e-9
        assert all(r["units"] > 0 for r in flat["lines"])


class TestCells:
    def test_one_lines_full_cells_dont_hold_back_another(self):
        # Every line uses Final Assembly: starve its cells and every line that
        # stops names it, with people left over.
        units = _units()
        crew = _crew(cells={"Final Assembly": 1})
        plan = _balance(units, crew, 400)
        stopped = {r["line"]: r["limit"] for r in plan["lines"]}
        assert set(stopped.values()) <= {"Final Assembly", ""}
        assert plan["spare"] > 0          # cells full → people left over
        assert sum(plan["hc"].values()) == 400

    def test_a_line_only_stops_at_its_own_cells(self):
        # PM is the only line with ETO work: starve ETO cells and only PM stops there.
        units = _units()
        plan = _balance(units, _crew(cells={"ETO": 1}), 400)
        lines = _by_line(plan)
        assert lines["PM"]["limit"] == "ETO" and lines["PM"]["share"] < 1.0
        for ln in ("EBOSS", "Generator", "Compressor"):
            assert lines[ln]["share"] == 1.0, ln

    def test_material_handling_is_never_stopped_by_cells(self):
        units = _units(support=50)
        plan = _balance(units, _crew(cells={"Material Handling": 0}), 600)
        assert all(r["limit"] != "Material Handling" for r in plan["lines"])
        assert plan["hc"]["Material Handling"] >= math.ceil(1500 * 50 / PER_PERSON_18)

    def test_excluded_station_cells_dont_stop_lines(self):
        units = _units()
        plan = _balance(units, _crew(cells={"ETO": 1}), 400, excluded_stations=["ETO"])
        assert _by_line(plan)["PM"]["share"] == 1.0
        assert plan["hc"]["ETO"] > 0     # still staffed for its work


class TestEdges:
    def test_empty_units(self):
        plan = _balance(pd.DataFrame(), _crew(), 10)
        assert plan["lines"] == [] and plan["spare"] == 10

    def test_zero_people(self):
        plan = _balance(_units(), _crew(), 0)
        assert all(r["units"] == 0 for r in plan["lines"])


# ---------------------------------------------------------------------------
# AppTest — the What-if button
# ---------------------------------------------------------------------------
app = pytest.importorskip("app")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


class TestWhatIfBalanceButton:
    @pytest.fixture
    def at(self, tmp_path, monkeypatch):
        import core.usage_tracker as usage_tracker
        monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.session_state[app.ACTIVE_TAB_KEY] = app.TAB_RECOMMENDATIONS
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        return at

    def test_balance_rewrites_people_and_shows_the_plan(self, at):
        loc = at.sidebar.selectbox[0].value
        next(n for n in at.main.number_input if n.key == f"whatif_people_{loc}").set_value(74)
        at.run()
        next(b for b in at.main.button if b.key == f"whatif_balance_{loc}").click()
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        scenario = at.session_state[f"_whatif_crew_{loc}"]
        assert int(scenario["People"].sum()) == 74
        plan_tables = [d.value for d in at.main.dataframe if "Units built" in d.value.columns]
        assert plan_tables, "⚖️ build plan table missing"
        starred = plan_tables[0][plan_tables[0]["First"] == "⭐"]["Line"].tolist()
        assert "Power Module" in starred
