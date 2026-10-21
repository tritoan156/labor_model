"""Only the tab in front is built — guards for the per-click cost work.

Before this, ``main()`` built all eight ``st.tabs`` panels on every rerun (the
server can't see which tab is showing), plus the five help tabs and the Admin
dashboard inside collapsed sidebar expanders. These tests pin the new shape:

  * the tab strip is a session-backed segmented control and only the chosen
    body renders;
  * the Data & Setup view picker survives a trip to another tab;
  * the help pages and the admin dashboard render only once toggled on;
  * the bodies with self-contained widgets are ``@st.fragment``s;
  * no global ``st.cache_data.clear()`` is left (each save clears its own
    loader) and the weekly-heatmap cache returns what the helper computes.

The AppTest flows drive the real script, one run per step, so they are the
slow part of the suite; related steps share one app instance.
"""
import inspect
import re
from pathlib import Path

import pandas as pd
import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
app = pytest.importorskip("app")

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

DATA_SETUP_HEADER = "📁 Data & Setup"
CAPACITY_HEADER = "📊 Capacity vs Demand"
DATA_QUALITY_HEADER = "🔍 Data Quality"
DATA_QUALITY_VIEW = "🔍 Data Quality"


def _run(at):
    """``at.run()``, seeding any session key the bare harness complains about
    (the sidebar reads a few keys the browser would normally have created)."""
    for _ in range(20):
        at.run()
        seeded = False
        for exc in at.exception:
            m = re.search(r'st\.session_state has no key "([^"]+)"', str(exc.value))
            if m:
                at.session_state[m.group(1)] = ""
                seeded = True
        if not seeded:
            return at
    raise AssertionError("could not seed session state for the app run")


def _headers(at) -> list:
    return [h.value for h in at.main.header] + [h.value for h in at.main.subheader]


def _infos(at) -> list:
    return [i.value for i in at.main.info]


def _strip(at):
    # AppTest lists a segmented control's options as the proto `content`, i.e.
    # with the leading emoji split off into the icon ("Overview", not
    # "🏠 Overview"); `.value` and `.set_value` use the real label.
    strips = [
        w for w in at.main.segmented_control
        if "Overview" in [str(o).strip() for o in w.options]
    ]
    assert len(strips) == 1, "exactly one top-level tab strip expected"
    return strips[0]


def _body_shown(at, header: str, empty_name: str) -> bool:
    """A tab body counts as rendered if its header is up, or its empty-state
    banner is (the bundled sample may be absent for a facility)."""
    return header in _headers(at) or any(
        f"{empty_name} needs a schedule" in msg for msg in _infos(at)
    )


def _no_errors(at):
    assert list(at.exception) == [], [str(e.value)[:200] for e in at.exception]


class TestStaticShape:
    def test_main_builds_one_body_through_the_tab_strip(self):
        src = inspect.getsource(app.main)
        assert "st.tabs(" not in src, "main() must not build every panel"
        assert "_render_tab_strip()" in src
        for label in app.TAB_LABELS:
            assert label in app.TAB_LABELS  # constants exist and are spelled once
        assert len(set(app.TAB_LABELS)) == 8

    def test_no_global_cache_clear_remains(self):
        offenders = [
            n for n, line in enumerate(APP_PATH.read_text(encoding="utf-8").splitlines(), 1)
            if line.strip() == "st.cache_data.clear()"
        ]
        assert offenders == [], f"global st.cache_data.clear() at lines {offenders}"

    @pytest.mark.parametrize("name", [
        "tab_exec_summary",
        "tab_process_flow",
        "tab_cycle_time",
        "tab_data_validation",
        "_render_help_glossary",
        "_render_admin_panel",
    ])
    def test_self_contained_bodies_are_fragments(self, name):
        fn = getattr(app, name)
        # st.fragment returns a wrapper defined in streamlit/runtime/fragment.py.
        assert hasattr(fn, "__wrapped__"), f"{name} is not decorated"
        assert Path(fn.__code__.co_filename).name == "fragment.py", (
            f"{name} is wrapped by something other than @st.fragment"
        )

    def test_catalog_loader_lookup_covers_the_four_csvs(self):
        assert app._catalog_loader_for("data/machine_clean.csv") is app._load_machine_df_cached
        assert app._catalog_loader_for("acc_clean.csv") is app._load_acc_df_cached
        assert app._catalog_loader_for("data/item_master.csv") is app._load_item_master_df_cached
        assert app._catalog_loader_for("data/item_packages.csv") is app._load_item_packages_df_cached
        assert app._catalog_loader_for("data/scenarios.json") is None
        # Clearing is best-effort and must never raise, even in bare mode.
        app._clear_catalog_cache("data/machine_clean.csv", "data/not_a_catalog.csv")


class TestOnlyActiveTabRenders:
    @pytest.fixture(scope="class")
    def at(self):
        return _run(AppTest.from_file(str(APP_PATH), default_timeout=300))

    def test_default_is_overview_and_other_bodies_are_absent(self, at):
        _no_errors(at)
        assert _strip(at).value == app.TAB_OVERVIEW
        assert at.session_state[app.ACTIVE_TAB_KEY] == app.TAB_OVERVIEW
        assert DATA_SETUP_HEADER not in _headers(at)
        assert not _body_shown(at, CAPACITY_HEADER, "Capacity")

    def test_switching_builds_only_the_chosen_body(self, at):
        _strip(at).set_value(app.TAB_DATA_SETUP)
        _run(at)
        _no_errors(at)
        assert DATA_SETUP_HEADER in _headers(at)
        assert not _body_shown(at, CAPACITY_HEADER, "Capacity")

        _strip(at).set_value(app.TAB_CAPACITY)
        _run(at)
        _no_errors(at)
        assert _body_shown(at, CAPACITY_HEADER, "Capacity")
        assert DATA_SETUP_HEADER not in _headers(at)

    def test_data_setup_view_survives_a_trip_to_another_tab(self, at):
        _strip(at).set_value(app.TAB_DATA_SETUP)
        _run(at)
        view = next(r for r in at.main.radio if r.label == "View")
        view.set_value(DATA_QUALITY_VIEW)
        _run(at)
        _no_errors(at)
        assert DATA_QUALITY_HEADER in _headers(at)

        _strip(at).set_value(app.TAB_OVERVIEW)
        _run(at)
        assert DATA_QUALITY_HEADER not in _headers(at)

        _strip(at).set_value(app.TAB_DATA_SETUP)
        _run(at)
        _no_errors(at)
        view = next(r for r in at.main.radio if r.label == "View")
        assert view.value == DATA_QUALITY_VIEW
        assert DATA_QUALITY_HEADER in _headers(at)

    def test_every_tab_renders_without_error(self, at):
        for label in app.TAB_LABELS:
            _strip(at).set_value(label)
            _run(at)
            _no_errors(at)
            assert _strip(at).value == label

    @pytest.mark.parametrize("view, subheader", [
        ("📤 Export & Excel link", "📤 Export & Excel link"),
        ("📥 Import from Excel", "📥 Import from Excel"),
    ])
    def test_excel_views_are_reachable_inside_data_setup(self, at, view, subheader):
        # The Excel views landed (PRs #10 and #12) while the tab strip was being
        # built; they must stay reachable through the remembered view picker.
        _strip(at).set_value(app.TAB_DATA_SETUP)
        _run(at)
        picker = next(r for r in at.main.radio if r.label == "View")
        assert view in list(picker.options)
        picker.set_value(view)
        _run(at)
        _no_errors(at)
        assert subheader in _headers(at)


class TestSidebarGates:
    def test_help_pages_render_only_when_toggled(self):
        at = _run(AppTest.from_file(str(APP_PATH), default_timeout=300))
        help_toggle = next(t for t in at.sidebar.toggle if t.label == "Show help")
        assert help_toggle.value is False
        assert [t.label for t in at.sidebar.tabs] == []

        help_toggle.set_value(True)
        _run(at)
        _no_errors(at)
        assert "🚀 Quick start" in [t.label for t in at.sidebar.tabs]

    def test_admin_dashboard_renders_only_when_toggled(self):
        at = _run(AppTest.from_file(str(APP_PATH), default_timeout=300))
        admin_toggle = next(t for t in at.sidebar.toggle if t.label == "Load analytics")
        assert admin_toggle.value is False
        assert list(at.sidebar.metric) == []
        assert not any("No usage events" in c.value for c in at.sidebar.caption)

        admin_toggle.set_value(True)
        _run(at)
        _no_errors(at)
        dashboard_up = (
            len(list(at.sidebar.metric)) >= 3
            or any("No usage events" in c.value for c in at.sidebar.caption)
        )
        assert dashboard_up


class TestWeeklyUtilizationCache:
    """The Overview heatmap helper now runs behind a content-keyed cache; the
    wrapper must unpack the sidebar dict faithfully."""

    def _schedule(self, days_qty):
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

    def _inputs(self, **over):
        crew = pd.DataFrame(
            {"HC": [3, 4], "Conc": [4, 4], "Crew": [1, 1]},
            index=["PM Acc (Headunit)", "PDI"],
        )
        base = {"crew_config": crew, "shift": 480, "days": 20, "safety": 1.0,
                "efficiency": 1.0, "new_hire_pct": 0.0,
                "new_hire_productivity": 1.0, "absenteeism": 0.0, "overtime": 0.0,
                # keys the real sidebar dict carries that must NOT reach the key
                "uploaded": object(), "manual_entries": pd.DataFrame({"x": [1]})}
        base.update(over)
        return base

    def test_wrapper_matches_direct_computation(self):
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        full = self._schedule([("2026-06-08", 10), ("2026-06-29", 4)])
        inputs = self._inputs()
        direct = app._compute_weekly_utilization(full, machine_df, acc_df, inputs)
        via_cache = app._weekly_utilization_for(full, machine_df, acc_df, inputs)
        assert direct is not None
        pd.testing.assert_frame_equal(via_cache, direct)
        # Second call is a hit and must return an equal frame, not a shared one.
        again = app._weekly_utilization_for(full, machine_df, acc_df, inputs)
        pd.testing.assert_frame_equal(again, direct)
        assert again is not via_cache

    def test_changed_setting_changes_the_result(self):
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        full = self._schedule([("2026-06-08", 10)])
        base = app._weekly_utilization_for(full, machine_df, acc_df, self._inputs())
        halved = app._weekly_utilization_for(
            full, machine_df, acc_df, self._inputs(efficiency=0.5),
        )
        assert float(halved.loc["PDI", "Wk 2"]) > float(base.loc["PDI", "Wk 2"])

    def test_empty_schedule_is_none(self):
        machine_df = app._load_machine_df(0.0)
        acc_df = app._load_acc_df(0.0)
        assert app._weekly_utilization_for(pd.DataFrame(), machine_df, acc_df, self._inputs()) is None
