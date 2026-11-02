"""The 🔧 Recommendations tab's 🧪 What-if simulator.

On 2026-11-02 the planner asked "if we have 73 people, what is our
recommended capacity", then which extra cells would unlock more. Both were
answered by pointing ``core.facility_storage.FACILITY_CREW_PATH`` at scratch
crew files and driving the app with AppTest. The simulator does it in the
app: a scenario copy of the crew (People, Stations/Cells) and a working-day
count, priced with the Capacity tab's own calculators, compared side by side
with the current crew, and copied into the sidebar's crew draft on request.

With Henderson's "Hen November 11/02" schedule (357 of 369 rows counted) the
panel reproduced the hand-run answers: 81 max units at 55 people / 21 days
(need 73); 237 at 73 people staffed to need, 203 at 18 days; 363 with cells
Battery 12 / Com Acc 3 / ETO 7 / Final 5; at 18 days with Battery 14 / Com
Acc 3 / ETO 8 / Final 6 / Gen Acc 14, 312 at 73 people and 362 at 85. Those
numbers move with every catalog and crew save, so the tests below pin the
rules instead: the helpers on a hand-built plan, and the panel's buttons,
warning and Apply on whatever Henderson plan the repo loads.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

import core.facility_storage as facility_storage
import core.usage_tracker as usage_tracker
from core.constants import STATION_KEYS, STATION_KEY_TO_DISPLAY, SUPPORT_ROLES

app = pytest.importorskip("app")

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
LOCATION = "Henderson"
KEYS = app._whatif_keys(LOCATION)


# ---------------------------------------------------------------------------
# A hand-built plan: 20 compressors (Com Accessories + QC) and 10 generators
# (Gen Accessories + Final Assembly), one person and one cell everywhere.
# ---------------------------------------------------------------------------
def _units() -> pd.DataFrame:
    rows = []
    for i in range(30):
        r = {k: 0 for k in STATION_KEYS}
        if i < 20:
            r.update({"fg_base": "PDS185-001", "ComAcc": 600, "QC": 120})
        else:
            r.update({"fg_base": "SDG65-001", "GenAcc": 900, "Final": 300})
        r.update({"Class": "STD", "Bat": 0, "unit_id": i + 1, "batt_type": ""})
        rows.append(r)
    return pd.DataFrame(rows)


def _crew() -> pd.DataFrame:
    idx = [STATION_KEY_TO_DISPLAY[k] for k in STATION_KEYS] + list(SUPPORT_ROLES)
    crew = pd.DataFrame({"HC": 1, "Conc": 1, "Crew": 1}, index=idx)
    crew.loc[list(SUPPORT_ROLES)] = [2, 0, 0]
    return crew


def _inputs(crew=None, days=20) -> dict:
    return {
        "location": LOCATION, "crew_config": _crew() if crew is None else crew,
        "shift": 480, "days": days, "safety": 1.0, "efficiency": 0.5,
        "new_hire_pct": 0.0, "new_hire_productivity": 1.0, "absenteeism": 0.0,
        "overtime": 0.0, "excluded_stations": [],
    }


class TestHelpers:
    def test_the_scenario_crew_keeps_support_roles_and_sanitises_cleared_cells(self):
        crew = _crew()
        scn = app._whatif_display(crew)
        assert list(scn.columns) == ["People", "Stations/Cells", "Crew per unit"]
        assert not set(SUPPORT_ROLES) & set(scn.index)
        scn = scn.astype(float)
        scn.loc["QC", "People"] = 7
        scn.loc["PDI", "People"] = float("nan")      # a cleared editor cell
        scn.loc["Ship", "Crew per unit"] = 0          # an invalid divide
        out = app._whatif_crew(scn, crew)
        assert list(out.index) == list(crew.index)
        assert out.loc["QC", "HC"] == 7
        assert out.loc["PDI", "HC"] == 0
        assert out.loc["Ship", "Crew"] == 1
        assert out.loc["Lead", "HC"] == 2 and out.loc["Other", "HC"] == 2
        assert all(str(t).startswith("int") for t in out.dtypes)

    def test_staff_to_need_closes_every_gap_and_matches_the_need(self):
        units, inputs = _units(), _inputs()
        cap = app._whatif_capacity(units, inputs["crew_config"], 20, inputs)
        scn = app._whatif_staff_to_need(app._whatif_display(inputs["crew_config"]), cap)
        out = app._whatif_outcome(units, app._whatif_crew(scn, inputs["crew_config"]), 20, inputs)
        assert out["people"] == out["need"] == int(cap["required_hc"].sum())
        assert (out["capacity"]["hc_gap"] <= 0).all()
        # Stations the plan never touches need nobody.
        assert scn.loc["Battery Assembly", "People"] == 0

    def test_add_cells_to_fit_brings_throughput_to_100_percent_or_less(self):
        units, inputs = _units(), _inputs()
        before = app._whatif_capacity(units, inputs["crew_config"], 20, inputs)
        over = before.index[before["thru_util_safe"] > 1].tolist()
        assert over, "the hand-built plan should overload a cell"
        scn = app._whatif_cells_to_fit(app._whatif_display(inputs["crew_config"]), before)
        after = app._whatif_capacity(units, app._whatif_crew(scn, inputs["crew_config"]), 20, inputs)
        assert (after["thru_util_safe"] <= 1 + 1e-9).all()
        for station in before.index:
            if station not in over:
                assert scn.loc[station, "Stations/Cells"] == 1
        # Throughput doesn't depend on people; labor doesn't depend on cells.
        assert after["labor_util_safe"].equals(before["labor_util_safe"])

    def test_cells_to_fit_rounds_away_float_noise(self):
        scn = pd.DataFrame({"People": [1], "Stations/Cells": [8], "Crew per unit": [1]},
                           index=["ETO"])
        cap = pd.DataFrame({"thru_util_safe": [1.5000000001]}, index=["ETO"])
        assert app._whatif_cells_to_fit(scn, cap).loc["ETO", "Stations/Cells"] == 12

    def test_the_outcome_quotes_the_capacity_tab_headline(self):
        units, inputs = _units(), _inputs()
        cap = app._whatif_capacity(units, inputs["crew_config"], 20, inputs)
        mu = app._max_units_at_current_mix(cap, len(units))
        bl = app.buildable_by_line(units, cap)
        out = app._whatif_outcome(units, inputs["crew_config"], 20, inputs)
        assert len(bl["lines"]) == 2
        assert out["max_units"] == bl["total"] == app._headline_max_units(mu, bl, len(units))
        assert out["units_per_day"] == pytest.approx(out["max_units"] / 20)
        assert out["labor_units"] == mu["labor_max_units"]
        assert out["thru_units"] == mu["thru_max_units"]

    def test_fewer_days_raise_the_need_and_cut_the_units(self):
        units, inputs = _units(), _inputs()
        at20 = app._whatif_outcome(units, inputs["crew_config"], 20, inputs)
        at15 = app._whatif_outcome(units, inputs["crew_config"], 15, inputs)
        assert at15["need"] > at20["need"]
        assert at15["max_units"] < at20["max_units"]

    def test_understaffed_cells_are_flagged_only_where_there_is_work(self):
        units, inputs = _units(), _inputs()
        cap = app._whatif_capacity(units, inputs["crew_config"], 20, inputs)
        scn = app._whatif_display(inputs["crew_config"])
        scn.loc["Com Accessories", ["People", "Stations/Cells"]] = [2, 3]
        scn.loc["QC", "People"] = 0
        scn.loc["Battery Assembly", ["People", "Stations/Cells"]] = [1, 9]   # no demand
        flagged = " ".join(app._whatif_understaffed(scn, cap))
        assert "**Com Accessories** (2 people < 3 cells × 1)" in flagged
        assert "**QC** (no people)" in flagged
        assert "Battery" not in flagged

    def test_headline_falls_back_like_the_capacity_tab(self):
        single = {"lines": [{"line": "PM"}], "total": 40}
        assert app._headline_max_units(
            {"potential_util_safe": 2.0, "potential_max_units": 25}, single, 50) == 25
        assert app._headline_max_units({"potential_util_safe": 0.0}, single, 50) == 50
        blocked = {"lines": [{"line": "PM"}, {"line": "EBOSS"}], "total": 0}
        assert app._headline_max_units(
            {"potential_util_safe": 2.0, "potential_max_units": 25}, blocked, 50) == 25


# ---------------------------------------------------------------------------
# The panel, driven through AppTest on the repo's Henderson plan
# ---------------------------------------------------------------------------
@pytest.fixture
def panel(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
    crew_mtime = facility_storage.FACILITY_CREW_PATH.stat().st_mtime
    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.session_state[app.ACTIVE_TAB_KEY] = app.TAB_RECOMMENDATIONS
    _run(at)
    if _compare(at) is None:
        pytest.skip(f"no {LOCATION} plan with units to price")
    yield at
    # Apply is a draft: nothing on disk changes.
    assert facility_storage.FACILITY_CREW_PATH.stat().st_mtime == crew_mtime


def _run(at):
    at.run()
    assert list(at.exception) == [], [str(e.value)[:300] for e in at.exception]


def _compare(at) -> "pd.DataFrame | None":
    for d in at.main.dataframe:
        if list(d.value.columns) == ["", "Current", "Scenario"]:
            return d.value.set_index("")
    return None


def _num(cell: str) -> int:
    return int(re.match(r"[\d,]+", str(cell)).group(0).replace(",", ""))


def _click(at, key: str):
    next(b for b in at.main.button if b.key == key).click()
    _run(at)


def _set_days(at, days: int):
    at.main.number_input(key=KEYS["days"]).set_value(days)
    _run(at)


def _metric(at, label: str) -> str:
    return next(str(m.value) for m in at.main.metric if m.label == label)


class TestWhatIfPanel:
    def test_it_opens_on_the_current_crew_and_matches_the_capacity_tab(self, panel):
        cmp = _compare(panel)
        assert (cmp["Current"] == cmp["Scenario"]).all(), cmp
        assert _num(cmp.at["Working days", "Current"]) == panel.session_state["work_days_input"]
        prod, _supp, _tot = app._split_crew_hc(app.load_facility_crew_df(LOCATION))
        assert _num(cmp.at["People (production)", "Current"]) == prod
        max_units = _num(cmp.at["Max units (all lines)", "Current"])

        panel.session_state[app.ACTIVE_TAB_KEY] = app.TAB_CAPACITY
        _run(panel)
        headline = next(m for m in panel.main.metric if m.label.startswith("Max units"))
        assert _num(headline.value) == max_units

    def test_staff_to_need_then_fewer_days_then_reset(self, panel):
        _click(panel, f"whatif_staff_{LOCATION}")
        cmp = _compare(panel)
        need = _num(cmp.at["Need (Required HC)", "Current"])
        assert _num(cmp.at["People (production)", "Scenario"]) == need
        assert _num(cmp.at["Max units (all lines)", "Scenario"]) >= _num(
            cmp.at["Max units (all lines)", "Current"])
        assert any("Required HC at" in c.value for c in panel.main.caption)
        staffed = _num(cmp.at["Max units (all lines)", "Scenario"])

        days = panel.session_state["work_days_input"]
        _set_days(panel, days - 3)
        cmp = _compare(panel)
        assert _num(cmp.at["Working days", "Scenario"]) == days - 3
        assert _num(cmp.at["People (production)", "Scenario"]) == need   # people kept
        assert _num(cmp.at["Need (Required HC)", "Scenario"]) >= need
        assert _num(cmp.at["Max units (all lines)", "Scenario"]) <= staffed

        _click(panel, f"whatif_reset_{LOCATION}")
        cmp = _compare(panel)
        assert (cmp["Current"] == cmp["Scenario"]).all(), cmp
        assert panel.session_state[KEYS["days"]] == days

    def test_add_cells_to_fit_clears_throughput_overloads(self, panel):
        saved = app.load_facility_crew_df(LOCATION)
        _click(panel, f"whatif_cells_{LOCATION}")
        assert any(c.value.startswith("🏭") for c in panel.main.caption)
        crew = app._whatif_crew(panel.session_state[KEYS["base"]], saved)
        assert (crew["Conc"] >= saved["Conc"]).all()
        assert crew["HC"].equals(saved["HC"])          # cells, not people
        cmp = _compare(panel)
        thru = cmp.at["Throughput-capped units", "Scenario"]
        assert thru == "—" or _num(thru) >= _num(cmp.at["Throughput-capped units", "Current"])
        # Labor is untouched by cells.
        assert cmp.at["Labor-capped units", "Scenario"] == cmp.at["Labor-capped units", "Current"]

    def test_too_few_people_for_the_cells_is_a_warning(self, panel):
        # The labor bottleneck certainly has work to do.
        m = re.search(r"\((.+)\)", _compare(panel).at["Labor-capped units", "Current"])
        if m is None:
            pytest.skip("no labor bottleneck in this plan")
        station = m.group(1)
        scn = panel.session_state[KEYS["base"]].copy()
        scn.loc[station, ["People", "Stations/Cells"]] = [1, 5]
        panel.session_state[KEYS["base"]] = scn   # what an edit in the table leaves
        _run(panel)
        warnings = [w.value for w in panel.main.warning
                    if "Can't confirm every cell can be staffed" in w.value]
        assert len(warnings) == 1, [w.value for w in panel.main.warning]
        assert f"**{station}** (1 people < 5 cells" in warnings[0]

    def test_apply_copies_the_scenario_into_the_unsaved_crew_draft(self, panel):
        _click(panel, f"whatif_staff_{LOCATION}")
        scn = panel.session_state[KEYS["base"]].copy()
        scenario_units = _num(_compare(panel).at["Max units (all lines)", "Scenario"])
        _click(panel, f"whatif_apply_{LOCATION}")

        draft = panel.session_state[f"_crew_draft_{LOCATION}"]
        assert list(draft.columns) == ["People", "Stations/Cells", "Crew per unit"]
        assert draft.loc[scn.index, "People"].tolist() == scn["People"].tolist()
        saved = app.load_facility_crew_df(LOCATION)
        assert draft.loc[list(SUPPORT_ROLES), "People"].tolist() == \
            saved.loc[list(SUPPORT_ROLES), "HC"].tolist()
        # The whole app now runs on it: current == scenario, and it says so.
        cmp = _compare(panel)
        assert (cmp["Current"] == cmp["Scenario"]).all(), cmp
        assert _num(cmp.at["Max units (all lines)", "Current"]) == scenario_units
        assert any("Save crew" in s.value for s in panel.main.success)
        sidebar = next(e for e in panel.sidebar.expander
                       if e.label.startswith("👥 Station headcount"))
        assert "Total headcount:** " + str(int(draft["People"].sum())) in " ".join(
            m.value for m in sidebar.markdown)
