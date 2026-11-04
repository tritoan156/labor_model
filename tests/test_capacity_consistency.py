"""A blocked station must mean the same thing in every buildable-unit view.

Use real capacity tables so zero-denominator guards, labor-only stations,
and independent production lines are exercised together.
"""
import pandas as pd
import pytest

import app
from core.constants import STATION_KEYS, STATION_KEY_TO_DISPLAY
from core.labor_calculator import build_capacity_table, buildable_by_line


def _plan(mixed=False):
    rows = []
    for fg, station in [("PDS185-001", "ComAcc"),
                        ("SDG65-001", "GenAcc") if mixed else ("PDS185-001", "ComAcc")]:
        row = {key: 0 for key in STATION_KEYS}
        row.update(fg_base=fg, Class="STD", Bat=0, QC=60, total_labor=180)
        row[station] = 120
        rows.append(row)
    units = pd.DataFrame(rows)
    crew = pd.DataFrame({"HC": 1, "Conc": 1, "Crew": 1},
                        index=list(STATION_KEY_TO_DISPLAY.values()))
    inputs = {"crew_config": crew, "shift": 480, "days": 1,
              "safety": 1.0, "efficiency": 1.0, "excluded_stations": []}
    return units, crew, inputs


def _capacity(units, crew):
    return build_capacity_table(units, crew, 480, 1, 1.0)


@pytest.mark.parametrize("column,reason", [
    ("HC", "no staff"), ("Conc", "no cells"), ("Crew", "invalid cycle"),
])
def test_single_line_zero_resource_blocks_all_buildable_views(column, reason):
    units, crew, inputs = _plan()
    crew.loc["Com Accessories", column] = 0
    cap = _capacity(units, crew)
    mix = app._max_units_at_current_mix(cap, len(units))
    lines = buildable_by_line(units, cap)
    assert "🔴" in cap.loc["Com Accessories", "overall_status"]
    assert mix["infeasible"] and mix["max_units"] == lines["total"] == 0
    assert mix["blocking_reasons"] == {"Com Accessories": reason}
    assert mix["potential_max_units"] == 8  # QC alone has room; build still blocked.
    assert app._headline_max_units(mix, lines, len(units)) == 0
    assert app._whatif_outcome(units, crew, 1, inputs)["max_units"] == 0


def test_independent_line_keeps_its_cap_when_other_line_has_no_cells():
    units, crew, inputs = _plan(mixed=True)
    crew.loc["Com Accessories", "Conc"] = 0
    cap = _capacity(units, crew)
    mix = app._max_units_at_current_mix(cap, len(units))
    lines = buildable_by_line(units, cap)
    assert mix["max_units"] == 0
    assert {r["line"]: r["buildable"] for r in lines["lines"]} == {
        "Compressor": 0, "Generator": 4,
    }
    assert app._headline_max_units(mix, lines, len(units)) == 4
    assert app._whatif_outcome(units, crew, 1, inputs)["max_units"] == 4


def test_all_lines_blocked_never_fall_back_to_positive_potential():
    units, crew, inputs = _plan(mixed=True)
    crew.loc[["Com Accessories", "Gen Accessories"], "Conc"] = 0
    cap = _capacity(units, crew)
    mix = app._max_units_at_current_mix(cap, len(units))
    assert mix["potential_max_units"] == 8
    assert app._headline_max_units(mix, buildable_by_line(units, cap), len(units)) == 0
    assert app._whatif_outcome(units, crew, 1, inputs)["max_units"] == 0


def test_exclusions_and_unused_stations_do_not_block_the_mix():
    units, crew, _ = _plan()
    crew.loc["Battery Assembly", ["HC", "Conc", "Crew"]] = 0  # no batteries
    crew.loc["Com Accessories", "Conc"] = 0
    cap = _capacity(units, crew)
    mix = app._max_units_at_current_mix(cap, len(units), ["Com Accessories"])
    assert not mix["infeasible"] and mix["max_units"] == 8
    assert mix["blocking_reasons"] == {}


def test_material_handling_needs_people_but_never_cells():
    units, crew, _ = _plan()
    # Use the canonical key rather than a synthetic station label.
    mh = next(k for k, v in STATION_KEY_TO_DISPLAY.items() if v == "Material Handling")
    units[mh] = 30
    crew.loc["Material Handling", ["Conc", "Crew"]] = 0
    cap = _capacity(units, crew)
    assert not app._max_units_at_current_mix(cap, len(units))["infeasible"]
    assert app._area_space_cap(cap.loc[["Material Handling"]], len(units)) is None
    crew.loc["Material Handling", "HC"] = 0
    mix = app._max_units_at_current_mix(_capacity(units, crew), len(units))
    assert mix["blocking_reasons"] == {"Material Handling": "no staff"}


def _capacity_panel(capacity, inputs, units):
    import app
    app.tab_capacity_vs_demand(capacity, inputs, total_units=len(units), units=units)


@pytest.mark.parametrize("all_blocked", [False, True])
def test_capacity_renders_zero_headline_and_names_missing_cells(all_blocked):
    from streamlit.testing.v1 import AppTest

    units, crew, inputs = _plan()
    crew.loc["Com Accessories", "Conc"] = 0
    if all_blocked:
        crew.loc["QC", "Conc"] = 0
    at = AppTest.from_function(_capacity_panel, args=(_capacity(units, crew), inputs, units)).run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label.startswith("Max units")) == "0"
    assert any("Com Accessories (no cells)" in w.value for w in at.warning)
    assert any(m.label == "Potential labor-capped units" for m in at.metric)
    areas = next(t.value for t in at.dataframe if "Blocked?" in t.value.columns)
    blocked = areas[areas["Blocked?"].str.contains("Com Accessories")]
    assert not blocked.empty and (blocked["Max units"] == 0).all()
    space = next(t.value for t in at.dataframe if "Max units/day (cells)" in t.value.columns)
    assert space.set_index("Station").loc["Com Accessories", "Mix ceiling (units)"] == 0
    space_areas = next(t.value for t in at.dataframe if "Limiting cell" in t.value.columns)
    assert space_areas["Mix ceiling (units)"].min() == 0


def test_capacity_warns_when_people_cannot_fill_configured_cells():
    from streamlit.testing.v1 import AppTest

    units, crew, inputs = _plan()
    crew.loc["Com Accessories", "Conc"] = 3
    at = AppTest.from_function(_capacity_panel, args=(_capacity(units, crew), inputs, units)).run()
    assert not at.exception
    assert any("Com Accessories** (1 people < 3 cells × 1)" in w.value for w in at.warning)


def _whatif_panel(inputs, units):
    import app
    app._render_whatif_simulator(units, inputs)


def test_whatif_marks_potential_caps_and_keeps_blocked_headline_at_zero():
    from streamlit.testing.v1 import AppTest

    units, crew, inputs = _plan()
    crew.loc["Com Accessories", "Conc"] = 0
    at = AppTest.from_function(_whatif_panel, args=(inputs, units)).run()
    assert not at.exception
    compare = next(t.value for t in at.dataframe if "Current" in t.value.columns).set_index("")
    assert compare.at["Max units (all lines)", "Current"] == "0"
    assert compare.at["Max units (all lines)", "Scenario"] == "0"
    assert compare.at["Labor-capped units", "Scenario"].endswith(" — potential")
    assert any("Scenario whole-mix cap: 0 units" in w.value for w in at.warning)
