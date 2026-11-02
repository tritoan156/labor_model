"""Material Handling — support work (receiving, inventory counts, internal
moves, anything else) sized as minutes per unit and staffed like a station.

Henderson asked for it on 2026-11-02: the production need (73 people at 21
days, 86 at 18) counted only people who put labor into a unit, and the
support crew was a flat "Other" headcount that never grew with volume. The
planner's minutes are 15 receiving + 5 counting + 15 internal movement + 15
other = 50 per unit.

What's pinned here:

* every expanded unit carries the facility's support minutes at
  ``MatHandling`` and in ``total_labor``; 0 minutes leaves the station idle;
* the station is labor-only — its Stations/Cells never limit it, even at 0;
* it sizes Required HC with the same factors as every other station, and an
  understaffed Material Handling caps every line through buildable_by_line;
* the minutes are saved per facility, cleaned, and kept by a save that
  doesn't send them;
* the sidebar inputs feed the Capacity tab (AppTest).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

import core.facility_settings_storage as fss
from core.constants import (
    LABOR_ONLY_STATIONS, STATION_DEFAULTS, STATION_KEY_TO_DISPLAY, STATION_KEYS,
    SUPPORT_FUNCTIONS,
)
from core.facility_settings_storage import (
    SUPPORT_MINUTES_MAX, clean_support_minutes, get_facility_settings,
    support_minutes_per_unit,
)
from core.labor_calculator import (
    build_capacity_table, buildable_by_line, expand_schedule,
    station_demand_table,
)

MH = "Material Handling"
HENDERSON_MINUTES = {"Receiving": 15, "Inventory counting": 5,
                     "Internal movement": 15, "Other support": 15}


def _catalog():
    machine = pd.DataFrame.from_dict({
        "BOSS25-006": {"Bat": 1, "Warehouse": 10, "Wire": 0, "Trailer": 0,
                       "QC": 5, "Ship": 0, "FN_Assy": 20, "PDI": 0, "ETO": 0},
        "PDS185EZ-001": {"Bat": 0, "Warehouse": 5, "Wire": 0, "Trailer": 0,
                         "QC": 10, "Ship": 0, "FN_Assy": 30, "PDI": 0, "ETO": 0},
    }, orient="index")
    for col, default in (("Final Station", "Final"), ("AccKIT Station", "AccKIT"),
                         ("PDI Station", "PDI"), ("Wire Station", "Wire"),
                         ("Undercarriage Station", "Undercarriage")):
        machine[col] = default
    acc = pd.DataFrame(columns=["BattSubRaw", "Nameplate Prep", "Warehouse",
                                "PMAcc", "GenAcc", "ComAcc", "AccKIT"])
    return machine, acc


def _schedule(boss=3, pds=2):
    rows = []
    for fg, qty in (("BOSS25-006", boss), ("PDS185EZ-001", pds)):
        if qty:
            rows.append({"BUILD QTY": qty, "FG_BASE": fg, "FG_RAW": fg, "ACC": "",
                         "DECAL": "", "CUSTOMER NAME": "", "CARRYOVER": False})
    return pd.DataFrame(rows)


def _crew(mh_hc=1, mh_conc=1, others=10):
    rows = {disp: (others, 10, 1) for disp in STATION_DEFAULTS}
    rows[MH] = (mh_hc, mh_conc, 1)
    return pd.DataFrame([{"HC": h, "Conc": c, "Crew": cr} for h, c, cr in rows.values()],
                        index=list(rows))


def _capacity(units, crew, days=18):
    return build_capacity_table(units, crew, 480, days, 1.0, 0.5,
                                new_hire_pct=0.2, new_hire_productivity=0.7,
                                absenteeism=0.05)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
class TestStationDefinition:
    def test_station_is_wired_into_every_table(self):
        assert "MatHandling" in STATION_KEYS
        assert STATION_KEY_TO_DISPLAY["MatHandling"] == MH
        assert MH in STATION_DEFAULTS
        assert "MatHandling" in LABOR_ONLY_STATIONS

    def test_ships_unstaffed(self):
        assert STATION_DEFAULTS[MH][0] == 0

    def test_four_support_functions(self):
        assert SUPPORT_FUNCTIONS == ("Receiving", "Inventory counting",
                                     "Internal movement", "Other support")


# ---------------------------------------------------------------------------
# expand_schedule
# ---------------------------------------------------------------------------
class TestExpandCarriesSupportMinutes:
    def test_every_unit_gets_the_minutes(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50)
        assert len(units) == 5
        assert (units["MatHandling"] == 50).all()

    def test_total_labor_includes_them(self):
        m, a = _catalog()
        base = expand_schedule(_schedule(), m, a)
        with_mh = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50)
        assert (with_mh["total_labor"] - base["total_labor"] == 50).all()

    def test_default_is_zero(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a)
        assert (units["MatHandling"] == 0).all()

    @pytest.mark.parametrize("bad", [None, "x", -5, float("nan")])
    def test_junk_minutes_are_zero(self, bad):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a, support_minutes_per_unit=bad)
        assert (units["MatHandling"] == 0).all()

    def test_folded_stations_move_to_material_handling(self):
        m, a = _catalog()
        base = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50)
        folded = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50,
                                 mh_folded_stations=("Warehouse", "Ship"))
        assert (folded["Warehouse"] == 0).all() and (folded["Ship"] == 0).all()
        moved = base["Warehouse"] + base["Ship"]
        assert (folded["MatHandling"] == 50 + moved).all()
        assert (folded["total_labor"] == base["total_labor"]).all()

    def test_only_foldable_stations_fold(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a, mh_folded_stations=("QC", "Bogus"))
        base = expand_schedule(_schedule(), m, a)
        assert units[["QC", "MatHandling"]].equals(base[["QC", "MatHandling"]])

    def test_demand_table_tolerates_a_unit_table_without_the_column(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a).drop(columns=["MatHandling"])
        assert station_demand_table(units).loc["MatHandling", "demand"] == 0


# ---------------------------------------------------------------------------
# build_capacity_table / buildable_by_line
# ---------------------------------------------------------------------------
class TestCapacity:
    def test_required_hc_uses_the_same_factors_as_production(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(boss=300, pds=57), m, a,
                                support_minutes_per_unit=50)
        cap = _capacity(units, _crew(mh_hc=6))
        row = cap.loc[MH]
        per_person = 480 * 18 * 0.5 * (1 - 0.2 * 0.3) * 0.95   # 3,857.76
        assert row["labor_demand"] == 357 * 50
        assert row["labor_cap_safe"] == pytest.approx(6 * per_person)
        assert row["required_hc"] == math.ceil(357 * 50 / per_person) == 5
        assert row["hc_gap"] == -1

    def test_labor_only_no_throughput_even_with_zero_cells(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50)
        for conc in (0, 1):
            row = _capacity(units, _crew(mh_conc=conc)).loc[MH]
            assert row["thru_cap_safe"] == 0
            assert row["thru_util_safe"] == 0
            assert row["thru_status_safe"] != "🔴"
        # Conc=0 doesn't make a labor-only station infeasible.
        no_cells = buildable_by_line(units, _capacity(units, _crew(mh_conc=0)))
        one_cell = buildable_by_line(units, _capacity(units, _crew(mh_conc=1)))
        assert no_cells["total"] == one_cell["total"] > 0

    def test_no_minutes_means_no_effect(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a)
        cap = _capacity(units, _crew(mh_hc=0))   # unstaffed is fine when idle
        assert cap.loc[MH, "labor_demand"] == 0
        without = _capacity(units, _crew().drop(index=MH))
        assert buildable_by_line(units, cap) == buildable_by_line(units, without)

    def test_understaffed_material_handling_caps_every_line(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(boss=200, pds=100), m, a,
                                support_minutes_per_unit=50)
        cap = _capacity(units, _crew(mh_hc=2))
        util = cap.loc[MH, "labor_util_safe"]
        assert util > 1
        bl = {r["line"]: r for r in buildable_by_line(units, cap)["lines"]}
        for line, planned in (("EBOSS", 200), ("Compressor", 100)):
            assert bl[line]["binding_station"] == MH
            assert bl[line]["buildable"] == int(planned // util)

    def test_unstaffed_with_minutes_blocks_like_any_station(self):
        m, a = _catalog()
        units = expand_schedule(_schedule(), m, a, support_minutes_per_unit=50)
        bl = buildable_by_line(units, _capacity(units, _crew(mh_hc=0)))
        assert bl["total"] == 0


# ---------------------------------------------------------------------------
# facility settings
# ---------------------------------------------------------------------------
class TestSupportMinutesSettings:
    def test_clean_fills_every_function(self):
        assert clean_support_minutes({"Receiving": 15}) == {
            "Receiving": 15.0, "Inventory counting": 0.0,
            "Internal movement": 0.0, "Other support": 0.0,
        }

    def test_clean_drops_junk(self):
        out = clean_support_minutes({"Receiving": -3, "Inventory counting": "x",
                                     "Internal movement": float("nan"),
                                     "Other support": 10_000, "Bogus": 9})
        assert out == {"Receiving": 0.0, "Inventory counting": 0.0,
                       "Internal movement": 0.0, "Other support": SUPPORT_MINUTES_MAX}
        assert clean_support_minutes(None) == clean_support_minutes("junk")

    def test_total(self):
        assert support_minutes_per_unit({"support_minutes": HENDERSON_MINUTES}) == 50
        assert support_minutes_per_unit({}) == 0
        assert support_minutes_per_unit(None) == 0

    def test_get_reads_saved_minutes(self, monkeypatch):
        monkeypatch.setattr(fss, "load_all_facility_settings",
                            lambda: {"Henderson": {"support_minutes": HENDERSON_MINUTES}})
        assert get_facility_settings("Henderson")["support_minutes"] == {
            k: float(v) for k, v in HENDERSON_MINUTES.items()}

    def test_folded_stations_are_cleaned(self):
        assert fss.clean_mh_folded(["Ship", "QC", "Warehouse (Pick)", "Ship"]) == [
            "Warehouse (Pick)", "Ship"]
        assert fss.clean_mh_folded("Ship") == []
        assert fss.mh_folded_station_keys(["Ship"]) == ("Ship",)
        assert fss.mh_folded_station_keys(None) == ()

    def test_get_defaults_to_zero(self, monkeypatch):
        monkeypatch.setattr(fss, "load_all_facility_settings", lambda: {})
        assert support_minutes_per_unit(get_facility_settings("Cypress")) == 0

    @pytest.fixture
    def github(self, monkeypatch, tmp_path):
        store = {"Henderson": {"safety": 1.0, "support_minutes": HENDERSON_MINUTES,
                               "mh_folded_stations": ["Warehouse (Pick)", "Ship"]}}
        pushed = []
        monkeypatch.setattr(fss, "_fetch_existing_from_github",
                            lambda token: (json.loads(json.dumps(store)), "sha"))
        monkeypatch.setattr(fss, "save_catalog_to_github",
                            lambda content, *a, **k: pushed.append(json.loads(content)) or {})
        monkeypatch.setattr(fss, "SETTINGS_PATH", tmp_path / "facility_settings.json")
        return pushed

    def test_save_writes_the_minutes(self, github):
        fss.save_facility_settings_to_github(
            "Henderson", {"support_minutes": {"Receiving": 20}}, "tok")
        assert github[-1]["Henderson"]["support_minutes"]["Receiving"] == 20.0
        assert github[-1]["Henderson"]["support_minutes"]["Other support"] == 0.0

    def test_save_without_minutes_keeps_the_saved_ones(self, github):
        fss.save_facility_settings_to_github("Henderson", {"safety": 0.9}, "tok")
        assert github[-1]["Henderson"]["support_minutes"] == {
            k: float(v) for k, v in HENDERSON_MINUTES.items()}
        assert github[-1]["Henderson"]["mh_folded_stations"] == ["Warehouse (Pick)", "Ship"]

    def test_save_writes_folded_stations(self, github):
        fss.save_facility_settings_to_github(
            "Henderson", {"mh_folded_stations": ["Ship"]}, "tok")
        assert github[-1]["Henderson"]["mh_folded_stations"] == ["Ship"]


# ---------------------------------------------------------------------------
# AppTest — the sidebar minutes reach the Capacity tab
# ---------------------------------------------------------------------------
app = pytest.importorskip("app")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


class TestSidebarDrivesCapacity:
    @pytest.fixture
    def at(self, tmp_path, monkeypatch):
        import core.usage_tracker as usage_tracker
        monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.session_state[app.ACTIVE_TAB_KEY] = app.TAB_CAPACITY
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        return at

    @staticmethod
    def _mh_row(at):
        for d in at.main.dataframe:
            v = d.value
            if "Labor used %" in v.columns and MH in v.index:
                return v.loc[MH]
        return None

    def test_minutes_put_load_on_material_handling(self, at):
        loc = at.sidebar.selectbox[0].value
        for fn, minutes in HENDERSON_MINUTES.items():
            box = next(n for n in at.sidebar.number_input
                       if n.key == f"support_min_{fn}_{loc}")
            box.set_value(float(minutes))
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        row = self._mh_row(at)
        assert row is not None, "Material Handling missing from the staffing table"
        assert int(row["Need"]) > 0

    def test_zero_minutes_and_nothing_folded_leave_it_idle(self, at):
        loc = at.sidebar.selectbox[0].value
        for fn in SUPPORT_FUNCTIONS:
            box = next(n for n in at.sidebar.number_input
                       if n.key == f"support_min_{fn}_{loc}")
            box.set_value(0.0)
        next(m for m in at.sidebar.multiselect
             if m.key == f"mh_folded_stations_{loc}").set_value([])
        at.run()
        row = self._mh_row(at)
        assert row is None or int(row["Need"]) == 0

    def test_folded_stations_load_material_handling_not_themselves(self, at):
        loc = at.sidebar.selectbox[0].value
        for fn in SUPPORT_FUNCTIONS:
            next(n for n in at.sidebar.number_input
                 if n.key == f"support_min_{fn}_{loc}").set_value(0.0)
        next(m for m in at.sidebar.multiselect
             if m.key == f"mh_folded_stations_{loc}").set_value(["Warehouse (Pick)", "Ship"])
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        staff = next(d.value for d in at.main.dataframe
                     if "Labor used %" in d.value.columns and MH in d.value.index)
        assert int(staff.loc[MH, "Need"]) > 0
        for st in ("Warehouse (Pick)", "Ship"):
            assert int(staff.loc[st, "Need"]) == 0
