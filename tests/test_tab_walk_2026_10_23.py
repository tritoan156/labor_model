"""Regression guards from the 2026-10-23 planner walk of the tabs the
2026-10-22 review skipped (Executive Summary, Calendar, Recommendations,
Build Time, Process Flow and every Data & Setup view), with Henderson's
newest saved schedule planned as its own month and then Cypress's.

What was wrong, in the order a planner met it:

* 📆 Calendar — "Week 6 (Aug 31) — 70 units" on a single day while every
  other day carried 13–14. Auto-spread balanced raw WEEK totals, so a
  partial first / last calendar week (1–4 working days) was handed a full
  week's units; the Overview heatmap then lit that week at 500 %+. The
  Suggest-a-schedule placer had the same key and priced every week as five
  days. Both now compare load per working day of the week.
* 🔀 Process Flow — the Accessory dropdown offered nothing but "(none)" for
  every SDG / PDS unit (two thirds of the catalog) and an HS unit could not
  pick its own HS accessory: the filter compared ``_machine_family`` (which
  collapses SDG65 → SDG) with the accessory hint (SDG65). Matching is now
  by model prefix, plus whatever the schedule pairs with the FG.
* ⏱ Build Time — "Avg labor / unit" was the mean over PAIRINGS (754 p-min)
  while the Overview's Total work ÷ Units planned and the Executive
  Summary's man-hours / unit said 776. It is qty-weighted now.
* 🧹 Customer suffix cleanup — a catalog SKU that merely ends in a suffix
  token (``BOSS25-018ES`` is its own catalog row) was listed as an orphan
  with "add the base SKU to the catalog first" advice. It is not flagged.
* 📋 Executive Summary — the 📥 Re-apply / 🗑 Delete picker listed scenarios
  alphabetically (opening on "Hen Aug 08/04") while the side-by-side below
  it defaulted to the newest save. Both now use saved_at, newest first.
* 📤 Export — the database workbook's "Saved schedules" sheet printed the
  stored row count (296 for a 288-row file; a quoted multi-line cell
  inflates a line-break count) that the 📂 picker had already stopped
  trusting. It recounts from the CSV.

The AppTest cases drive the real script (one run per step); the helpers
are tested directly.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path

import pandas as pd
import pytest

app = pytest.importorskip("app")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from core import excel_io  # noqa: E402
from core.constants import STATION_KEYS, STATION_KEY_TO_DISPLAY, week_of_month  # noqa: E402
from core.labor_calculator import compute_unit_labor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
LOCATION = "Henderson"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def catalogs():
    machine_df = app._project_facility_routing(
        app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
    )
    acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
    return machine_df, acc_df


@pytest.fixture
def at(tmp_path, monkeypatch):
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")

    def open_tab(tab: str):
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.session_state[app.ACTIVE_TAB_KEY] = tab
        at.run()
        assert not at.exception, [str(e.value)[:300] for e in at.exception]
        return at

    return open_tab


def _crew(hc: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        {"HC": hc, "Conc": hc, "Crew": 1},
        index=[STATION_KEY_TO_DISPLAY[k] for k in STATION_KEYS],
    )


def _undated(sku: str, n: int, month_label: str) -> pd.DataFrame:
    """``n`` single-unit rows with no PRODUCTION DAY — what an ERP export
    without dates looks like after the loader."""
    return pd.DataFrame({
        "FG SKU ID": [sku] * n, "FG_BASE": [sku] * n,
        "FG ACCRY SKU ID": [""] * n, "ACC": [""] * n,
        "BUILD QTY": [1] * n, "PRODUCTION MONTH": [month_label] * n,
        "PRODUCTION DAY": [pd.NaT] * n,
    })


def _units_per_day(df: pd.DataFrame) -> pd.Series:
    days = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce")
    return df.groupby(days.dt.date)["BUILD QTY"].sum()


def _metric(at, label: str) -> str:
    for m in at.main.metric:
        if m.label == label:
            return str(m.value)
    raise AssertionError(f"metric {label!r} not rendered: {[m.label for m in at.main.metric]}")


# ---------------------------------------------------------------------------
# 1. Auto-spread and the Suggest placer honour partial calendar weeks
# ---------------------------------------------------------------------------
class TestLevelLoadingPartialWeeks:
    """August 2026 runs Mon 3 … Fri 28 in four full weeks and ends on a lone
    Monday the 31st (week 6, one working day). July 2026 opens on a
    Wednesday (week 1: three days). February 2027 is four exact weeks."""

    @staticmethod
    def _labor(catalogs, sku: str) -> dict:
        machine_df, acc_df = catalogs
        if sku not in set(machine_df["SKU"].astype(str)):
            return {}
        labor = compute_unit_labor(sku, None, machine_df, acc_df) or {}
        return {k: float(labor.get(k, 0) or 0) for k in STATION_KEYS}

    @pytest.fixture
    def one_sku(self, catalogs):
        """A catalog SKU with real labor (a zero-labor row never moves the
        running loads, so every unit would land on the first day)."""
        for sku in ("SDG65-005", "BOSS70-001", "PDS185EZ-003", "SDG25-001"):
            if sum(self._labor(catalogs, sku).values()) > 0:
                return sku
        pytest.skip("no catalog SKU with labor to spread")

    @pytest.fixture
    def heavy_sku(self, catalogs):
        """A SKU whose busiest station needs more than one 480-minute day but
        less than a five-day week of one person (BOSS400HS-004: ETO 960)."""
        for sku in ("BOSS400HS-004", "BOSS220-001", "BOSS125-004"):
            lab = self._labor(catalogs, sku)
            if lab and 480 < max(lab.values()) <= 2400:
                return sku
        pytest.skip("no catalog SKU with a 480 < station labor <= 2400 profile")

    def test_the_lone_monday_gets_one_day_of_units_not_a_week(self, catalogs, one_sku):
        machine_df, acc_df = catalogs
        df = app._auto_spread_dates(
            _undated(one_sku, 210, "Aug-26"), machine_df, acc_df, LOCATION,
            target_month_hint=(2026, 8),
        )
        per_day = _units_per_day(df)
        assert len(per_day) == 21                      # every working day used
        assert per_day.max() - per_day.min() <= 1      # level: 10 each
        assert per_day[dt.date(2026, 8, 31)] == 10     # was 42 (a fifth of the month)

    def test_a_three_day_opening_week_carries_three_days(self, catalogs, one_sku):
        machine_df, acc_df = catalogs
        df = app._auto_spread_dates(
            _undated(one_sku, 230, "Jul-26"), machine_df, acc_df, LOCATION,
            target_month_hint=(2026, 7),
        )
        per_day = _units_per_day(df)
        assert len(per_day) == 23
        assert per_day.max() - per_day.min() <= 1
        by_week = df.groupby(pd.to_datetime(df["PRODUCTION DAY"]).dt.date.map(week_of_month))["BUILD QTY"].sum()
        assert by_week[1] == 30 and by_week[2] == 50   # 3 days vs 5 days

    def test_full_weeks_are_still_balanced_week_first(self, catalogs, one_sku):
        machine_df, acc_df = catalogs
        # 4 units over 4 exact weeks → one per week, not four in week 1.
        df = app._auto_spread_dates(
            _undated(one_sku, 4, "Feb-27"), machine_df, acc_df, LOCATION,
            target_month_hint=(2027, 2),
        )
        weeks = pd.to_datetime(df["PRODUCTION DAY"]).dt.date.map(week_of_month)
        assert sorted(weeks) == [1, 2, 3, 4]

    def test_pinned_rows_still_seed_the_load(self, catalogs, one_sku):
        machine_df, acc_df = catalogs
        df = _undated(one_sku, 20, "Aug-26")
        df.loc[:9, "PRODUCTION DAY"] = pd.Timestamp("2026-08-31")  # ten pinned on the Monday
        out = app._auto_spread_dates(df, machine_df, acc_df, LOCATION, target_month_hint=(2026, 8))
        per_day = _units_per_day(out)
        assert per_day[dt.date(2026, 8, 31)] == 10     # the pins stay, nothing is added
        assert out.attrs["auto_spread"]["count"] == 10

    def test_the_suggest_placer_uses_the_same_rule(self, catalogs, one_sku):
        machine_df, acc_df = catalogs
        inputs = {"plan_month": (2026, 8), "crew_config": _crew(50),
                  "shift": 480, "efficiency": 1.0, "safety": 1.0}
        new_df, overloads = app._suggest_schedule_dates(
            _undated(one_sku, 210, "Aug-26"), machine_df, acc_df, inputs,
        )
        per_day = _units_per_day(new_df)
        assert overloads == []
        assert len(per_day) == 21 and per_day.max() - per_day.min() <= 1
        assert per_day[dt.date(2026, 8, 31)] == 10

    def test_the_suggest_placer_prices_a_short_week_short(self, catalogs, heavy_sku):
        """A station's weekly capacity is HC × shift × THAT week's days. One
        person (480 min/day) can absorb a 960-minute station visit in a
        five-day week but never on the lone Monday — so every unit the
        placer is forced to put on Aug 31 is reported as an overload there.
        The flat five-day pricing let two such units sit on that Monday
        unflagged (and the Calendar showed four days of work on one day)."""
        machine_df, acc_df = catalogs
        inputs = {"plan_month": (2026, 8), "crew_config": _crew(1),
                  "shift": 480, "efficiency": 1.0, "safety": 1.0}
        new_df, overloads = app._suggest_schedule_dates(
            _undated(heavy_sku, 21, "Aug-26"), machine_df, acc_df, inputs,
        )
        per_day = _units_per_day(new_df)
        on_monday = int(per_day.get(dt.date(2026, 8, 31), 0))
        flagged_wk6 = sum(1 for o in overloads if o["week"] == 6)
        assert on_monday >= 1                      # the overflow has to go somewhere
        assert flagged_wk6 == on_monday            # … and each one is flagged there
        assert on_monday < max(per_day[d] for d in per_day.index if d != dt.date(2026, 8, 31)) + 2


# ---------------------------------------------------------------------------
# 2. Process Flow offers the accessories that belong to the unit
# ---------------------------------------------------------------------------
class TestProcessFlowAccessoryOptions:
    @pytest.mark.parametrize("sku, prefix", [
        ("SDG65-005", "SDG65"), ("BOSS70HS-001", "BOSS70HS"), ("PDS185EZ-003", "PDS185EZ"),
        ("SDG65S XXX", "SDG65S"), ("BOSS25-25 XXX", "BOSS25"), ("TBA", "TBA"),
        ("", ""), (None, ""), ("  sdg40-011 ", "SDG40"),
    ])
    def test_fg_model_prefix(self, sku, prefix):
        assert app._fg_model_prefix(sku) == prefix

    @pytest.mark.parametrize("a, b, ok", [
        ("SDG65", "SDG65", True), ("BOSS70", "BOSS70HS", True), ("BOSS70HS", "BOSS70", True),
        ("PDS185", "PDS185EZ", True), ("SDG40", "SDG400", False), ("SDG400", "SDG40", False),
        ("SDG65", "SDG65S", True), ("BOSS25", "BOSS25-25 AXXX", True),
        ("SDG", "SDG65", False), ("", "SDG65", False), ("SDG65", "", False),
        ("BOSS25", "BOSS70", False),
    ])
    def test_families_compatible(self, a, b, ok):
        assert app._families_compatible(a, b) is ok

    def test_sdg_and_pds_units_get_their_accessories(self, catalogs):
        _machine, acc_df = catalogs
        sdg = app._flow_accessory_options("SDG65-005", acc_df)
        assert "SDG65-A035" in sdg and all(s.startswith("SDG65") for s in sdg)
        pds = app._flow_accessory_options("PDS185EZ-003", acc_df)
        assert "PDS185EZ-A010" in pds and all(s.startswith("PDS185EZ") for s in pds)

    def test_hs_units_get_hs_and_base_accessories_but_not_other_models(self, catalogs):
        _machine, acc_df = catalogs
        hs = app._flow_accessory_options("BOSS70HS-001", acc_df)
        assert "BOSS70HS-A001" in hs and "BOSS70-A004" in hs
        base = app._flow_accessory_options("BOSS70-001", acc_df)
        assert "BOSS70HS-A001" in base
        assert not any(s.startswith("BOSS25") for s in base)
        assert not any(s.startswith("SDG40-") for s in app._flow_accessory_options("SDG400-001", acc_df))

    def test_schedule_pairings_are_offered_even_without_a_family(self, catalogs):
        _machine, acc_df = catalogs
        units = pd.DataFrame({"fg_base": ["BOSS125-004", "SDG65-005"], "acc": ["5000000214", ""]})
        assert "5000000214" in app._flow_accessory_options("BOSS125-004", acc_df, units)
        assert "5000000214" not in app._flow_accessory_options("SDG65-005", acc_df, units)
        assert "" not in app._flow_accessory_options("SDG65-005", acc_df, units)
        assert app._flow_accessory_options("10250", acc_df, units) == []

    def test_the_tab_offers_them(self, at):
        a = at(app.TAB_PROCESS_FLOW)
        a.selectbox(key="flow_fg").set_value("SDG65-005").run()
        assert not a.exception
        options = list(a.selectbox(key="flow_acc").options)
        assert options[0] == "(none)" and "SDG65-A035" in options
        a.selectbox(key="flow_acc").set_value("SDG65-A035").run()
        assert not a.exception
        assert any("Gen Accessories" in str(d.value.get("Station", "")) for d in a.main.dataframe)


# ---------------------------------------------------------------------------
# 3. Build Time's per-unit averages weigh each pairing by its units
# ---------------------------------------------------------------------------
class TestBuildTimeAverages:
    def test_weighted_by_qty(self):
        df = pd.DataFrame({"Total Labor (p-min)": [100, 10], "Lead Time (days)": [1.0, 0.1],
                           "Qty in schedule": [1, 9]})
        assert app._qty_weighted_means(df) == (19.0, pytest.approx(0.19))

    def test_falls_back_to_the_plain_mean_without_quantities(self):
        df = pd.DataFrame({"Total Labor (p-min)": [100, 10], "Lead Time (days)": [1.0, 0.1],
                           "Qty in schedule": [0, 0]})
        assert app._qty_weighted_means(df) == (55.0, pytest.approx(0.55))
        assert app._qty_weighted_means(df.drop(columns="Qty in schedule")) == (55.0, pytest.approx(0.55))
        assert app._qty_weighted_means(pd.DataFrame()) == (0.0, 0.0)

    def test_the_metric_is_total_work_over_units(self, at):
        a = at(app.TAB_BUILD_TIME)
        frames = [d.value for d in a.main.dataframe if "Qty in schedule" in list(d.value.columns)]
        if not frames:
            pytest.skip("no schedule expanded for the default facility")
        summary = frames[0]
        units = float(summary["Qty in schedule"].sum())
        expected = float((summary["Total Labor (p-min)"] * summary["Qty in schedule"]).sum()) / units
        shown = _metric(a, "Avg labor / unit")
        assert shown.startswith(f"{expected:,.0f} p-min"), (shown, expected)
        # And it is NOT the mean over pairings unless the two happen to agree.
        plain = float(summary["Total Labor (p-min)"].mean())
        if abs(plain - expected) >= 1:
            assert not shown.startswith(f"{plain:,.0f} p-min")


# ---------------------------------------------------------------------------
# 4. Suffix cleanup does not call a catalog SKU an orphan
# ---------------------------------------------------------------------------
class TestSuffixCleanupOrphans:
    @staticmethod
    def _schedule(*skus):
        return pd.DataFrame({"FG SKU ID": list(skus), "FG ACCRY SKU ID": [""] * len(skus)})

    def test_a_catalog_sku_ending_in_a_suffix_token_is_not_flagged(self):
        found = app._find_customer_suffixed_skus(
            self._schedule("BOSS25-018ES", "BOSS25-008 HERC"),
            machine_skus={"BOSS25-018ES", "BOSS25-008 HERC"}, acc_skus=set(),
        )
        assert found.empty

    def test_a_real_suffix_on_a_catalog_base_is_strippable(self):
        found = app._find_customer_suffixed_skus(
            self._schedule("BOSS70-001HRC", "BOSS70-001HRC", "BOSS70-001"),
            machine_skus={"BOSS70-001"}, acc_skus=set(),
        )
        assert found.to_dict("records") == [{
            "Original": "BOSS70-001HRC", "Suffix": "HRC", "Base": "BOSS70-001",
            "Type": "FG", "In catalog": "✓", "Rows": 2,
        }]

    def test_a_true_orphan_is_still_reported(self):
        found = app._find_customer_suffixed_skus(
            self._schedule("BOSS99-001UR"), machine_skus={"BOSS70-001"}, acc_skus=set(),
        )
        assert found["In catalog"].tolist() == ["✗"]
        assert found["Base"].tolist() == ["BOSS99-001"]


# ---------------------------------------------------------------------------
# 5. Executive scenarios: one order, newest first
# ---------------------------------------------------------------------------
class TestExecScenarioOrder:
    def test_newest_first_with_name_as_tie_break(self, monkeypatch):
        blocks = {
            "Alpha": {"saved_at": "2026-06-01T10:00:00-07:00"},
            "Zulu": {"saved_at": "2026-09-01T10:00:00-07:00"},
            "Mike": {"saved_at": "2026-09-01T10:00:00-07:00"},
            "NoDate": {},
        }
        monkeypatch.setattr(app, "list_exec_scenarios", lambda loc: sorted(blocks))
        monkeypatch.setattr(app, "get_exec_scenario", lambda loc, n: blocks.get(n))
        assert app._exec_scenarios_newest_first("X") == ["Zulu", "Mike", "Alpha", "NoDate"]

    def test_the_tab_uses_it_for_both_pickers(self, at):
        names = app.list_exec_scenarios(LOCATION)
        if len(names) < 2:
            pytest.skip(f"fewer than two saved executive scenarios for {LOCATION}")
        a = at(app.TAB_EXEC_SUMMARY)
        if not any("Save this scenario" in m.value for m in a.main.markdown):
            pytest.skip("no live schedule for the default facility")
        expected = app._exec_scenarios_newest_first(LOCATION)
        assert expected != sorted(names), "the data no longer tells the two orders apart"
        pick = a.selectbox(key=f"exec_pick_{LOCATION}")
        assert list(pick.options) == expected and pick.value == expected[0]
        cmp_ = a.selectbox(key=f"exec_cmp_{LOCATION}")
        assert list(cmp_.options) == ["—"] + expected and cmp_.value == expected[0]


# ---------------------------------------------------------------------------
# 6. The database workbook's saved-schedule index counts rows the same way
# ---------------------------------------------------------------------------
class TestSavedSchedulesIndexRows:
    def test_recounts_from_the_csv(self):
        quoted = 'LOCATION,FG SKU ID,NOTES\nHENDERSON,A,"line one\nline two"\nHENDERSON,B,x\n'
        idx = excel_io.saved_schedules_index({"Henderson": {
            "Old": {"saved_at": "t1", "rows": 3, "csv_content": quoted},   # stored count counted the line break
            "Blank": {"saved_at": "t2", "rows": 7, "csv_content": ""},
            "NoCsv": {"saved_at": "t3", "rows": 5},
        }})
        assert idx.set_index("Schedule")["Rows"].to_dict() == {"Old": 2, "Blank": 7, "NoCsv": 5}

    def test_matches_the_picker_for_the_bundled_data(self):
        from core.uploaded_schedule_storage import count_csv_data_rows, load_all_uploaded_schedules
        uploaded = load_all_uploaded_schedules()
        if not uploaded:
            pytest.skip("no saved schedules bundled")
        idx = excel_io.saved_schedules_index(uploaded).set_index(["Facility", "Schedule"])["Rows"]
        for fac, by_name in uploaded.items():
            for name, block in by_name.items():
                assert idx[(fac, name)] == count_csv_data_rows(block.get("csv_content", ""))
