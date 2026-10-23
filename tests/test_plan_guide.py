"""The plan guide: one panel at the top of the page that walks the workflow.

Steps 1–5 (schedule → month → catalog → capacity → save) are built by
functions that return data, so each state is pinned without a browser; the
AppTest flows prove the panel renders once, that the buttons the tests of
earlier PRs search for still resolve by prefix, and that the jumps land —
a tab, a Data & Setup view, the sidebar's crew expander — without touching
the settings a sidebar rerun used to wipe.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
LOCATION = "Henderson"


@pytest.fixture(autouse=True)
def _fresh_session_state(monkeypatch):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


@pytest.fixture(scope="module")
def catalogs():
    machine_df = app._project_facility_routing(
        app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
    )
    acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
    return machine_df, acc_df


@pytest.fixture
def inputs():
    crew = app.load_facility_crew_df(LOCATION)
    return {
        "location": LOCATION, "schedule_mode": app.SCHEDULE_MODE_UPLOAD,
        "plan_month": (2026, 10), "plan_month_label": "October 2026",
        "crew_config": crew, "uploaded": None,
    }


def _labels(step: dict) -> list:
    return [a["label"] for a in step["actions"]]


# ---------------------------------------------------------------------------
# Step 1 — schedule
# ---------------------------------------------------------------------------
class TestScheduleStep:
    def test_manual_mode_empty_and_filled(self, inputs):
        inputs["schedule_mode"] = app.SCHEDULE_MODE_MANUAL
        step = app._guide_schedule_step(inputs, pd.DataFrame(), None)
        assert step["status"] == "todo" and "Paste or type SKUs" in step["fact"]
        df = pd.DataFrame({"FG SKU ID": ["A", "B"], "BUILD QTY": [2, 3]})
        step = app._guide_schedule_step(inputs, df, None)
        assert step["status"] == "ok" and "2 row(s), 5 unit(s)" in step["fact"]
        assert _labels(step) == []
        st.session_state["_manual_schedule_override"] = {"sig": "s", "csv": b"x"}
        step = app._guide_schedule_step(inputs, df, None)
        assert "reshaped" in step["fact"] and _labels(step) == ["↩️ Back to the list as typed"]

    def test_failed_upload_offers_every_way_back(self, inputs):
        st.session_state["_schedule_before_error"] = {"csv": b"x"}
        step = app._guide_schedule_step(inputs, pd.DataFrame(), "no header found.")
        assert step["status"] == "error" and "Couldn't read the upload" in step["fact"]
        labels = _labels(step)
        assert labels[0] == "⏮ Back to the previous schedule"
        assert "🧪 Use the bundled sample" in labels
        assert ("📂 Load the newest saved schedule" in labels) == (
            app._newest_saved_schedule(LOCATION) is not None
        )

    def test_no_rows_for_the_facility(self, inputs):
        step = app._guide_schedule_step(inputs, pd.DataFrame(), None)
        assert step["status"] == "warn" and f"No rows for {LOCATION}" in step["fact"]
        assert "🧪 Use the bundled sample" in _labels(step)

    def test_loaded_counts_dropped_units_and_edits(self, inputs):
        df = pd.DataFrame({"FG SKU ID": ["A"], "BUILD QTY": [1]})
        st.session_state["_last_upload_summary"] = {
            "rows": 277, "source_rows": 288, "source": "saved", "name": "Hen Aug",
            "auto_loaded": True, "no_qty_rows": 11,
            "recovered": {"unmatched": {"Trailers": 2}, "no_model": 160, "count": 0},
        }
        step = app._guide_schedule_step(inputs, df, None)
        assert step["status"] == "warn"
        assert "**Hen Aug**" in step["fact"] and "loaded automatically" in step["fact"]
        assert "277 of 288 rows counted" in step["fact"]
        assert "**162 unit(s) in the file are not counted**" in step["fact"]
        assert step["details_title"].startswith("Why 277 of 288 rows?")
        assert any("11 row(s) had no BUILD QTY" in line for line in step["details"])
        assert any("162" not in line and "160 unit(s) are not counted" in line for line in step["details"])
        assert _labels(step) == []

        st.session_state["_schedule_edit_count"] = 2
        st.session_state["_schedule_undo"] = {"csv": b"x", "label": "Moved 3 row(s) into October 2026"}
        st.session_state["_schedule_original"] = {"csv": b"y"}
        step = app._guide_schedule_step(inputs, df, None)
        assert "2 change(s) since load" in step["fact"]
        assert _labels(step) == ["↩️ Undo: Moved 3 row(s) into October 2026", "⏮ Back to as loaded"]

    def test_an_upload_says_it_lives_in_the_browser(self, inputs):
        df = pd.DataFrame({"FG SKU ID": ["A"], "BUILD QTY": [1]})
        st.session_state["_last_upload_summary"] = {"rows": 1, "source": "upload", "name": "plan.xlsx"}
        step = app._guide_schedule_step(inputs, df, None)
        assert step["status"] == "ok" and "**plan.xlsx** (in this browser only)" in step["fact"]


# ---------------------------------------------------------------------------
# Step 2 — month
# ---------------------------------------------------------------------------
def _sched(month: str, day=None, n=3) -> pd.DataFrame:
    df = pd.DataFrame({
        "LOCATION": ["HENDERSON"] * n, "FG SKU ID": ["BOSS25-010"] * n,
        "FG ACCRY SKU ID": [""] * n, "BUILD QTY": [1] * n,
        "PRODUCTION MONTH": [month] * n,
        "PRODUCTION DAY": pd.to_datetime([day] * n) if day else pd.NaT,
        "CARRYOVER": [False] * n,
    })
    return df


class TestMonthStep:
    def test_matching_month_is_one_green_line(self, inputs, catalogs):
        df = _sched("Oct-26", "2026-10-06")
        step = app._guide_month_step(inputs, df, *catalogs)
        assert step["status"] == "ok" and step["fact"].startswith("Planning **October 2026**")
        assert _labels(step) == []

    def test_off_month_file_offers_plan_instead_and_move(self, inputs, catalogs):
        df = _sched("Aug-26", "2026-08-10")   # dated in August, planned as October
        step = app._guide_month_step(inputs, df, *catalogs)
        assert step["status"] == "warn"
        assert "the file is dated **August 2026**" in step["fact"]
        assert "3 row(s) aren't on the October 2026 calendar" in step["fact"]
        assert "plan **August 2026** instead" in step["fact"]
        labels = _labels(step)
        assert labels == ["📆 Plan August 2026 instead", "📅 Move 3 row(s) into October 2026"]
        assert [a["key"] for a in step["actions"]] == ["plan_schedule_month_btn", "move_into_plan_month_btn"]

    def test_auto_leveled_and_window_note_are_facts(self, inputs, catalogs):
        df = _sched("Oct-26", "2026-10-06")
        df.attrs["auto_spread"] = {"count": 3, "total_rows": 3, "target_month_label": "October 2026", "working_days": 22}
        step = app._guide_month_step(inputs, df, *catalogs, window_note="📅 **No units scheduled for week 4** in the plan.")
        assert "3 of 3 rows had no PRODUCTION DAY" in step["fact"]
        assert "No units scheduled for week 4" in step["fact"]
        assert step["status"] == "warn"

    def test_the_switch_button_seeds_the_picker(self, inputs, catalogs):
        src = inspect.getsource(app._guide_month_step)
        assert "PLAN_MONTH_SEED_KEY" in src and 'key="plan_schedule_month_btn"' in src
        assert "_commit_reshape(" in src


# ---------------------------------------------------------------------------
# Step 3 — catalog
# ---------------------------------------------------------------------------
class TestCatalogStep:
    def test_all_known(self, inputs, catalogs):
        df = _sched("Oct-26")
        df["FG_BASE"] = "BOSS25-010"
        df["ACC"] = ""
        step = app._guide_catalog_step(df, *catalogs, inputs)
        assert step["status"] == "ok" and "Every SKU in this plan is in the catalogs" in step["fact"]
        assert _labels(step) == []

    def test_missing_skus_offer_import_and_the_catalog(self, inputs, catalogs, monkeypatch):
        df = _sched("Oct-26")
        df["FG_BASE"] = ["NOPE-001", "NOPE-001", "BOSS25-010"]
        df["ACC"] = ["NOPE-A1", "", ""]
        monkeypatch.setattr(app, "_github_token", lambda: "tok")
        step = app._guide_catalog_step(df, *catalogs, inputs)
        assert step["status"] == "warn"
        assert "**1 FG SKU(s)** aren't in the machine catalog" in step["fact"]
        assert "**1 accessory SKU(s)** aren't in the accessory catalog" in step["fact"]
        assert _labels(step) == ["➕ Import 2 missing SKU(s) into the catalogs", "🏭 Open Machine catalog"]
        assert step["actions"][0]["disabled"] is False
        assert step["details_df"] is not None, "the routing preview rides in the details"

    def test_without_a_token_import_is_disabled_and_says_why(self, inputs, catalogs, monkeypatch):
        df = _sched("Oct-26")
        df["FG_BASE"] = "NOPE-001"
        df["ACC"] = ""
        monkeypatch.setattr(app, "_github_token", lambda: None)
        step = app._guide_catalog_step(df, *catalogs, inputs)
        assert step["actions"][0]["disabled"] is True
        assert any("needs the GitHub token" in line for line in step["details"])

    def test_imported_placeholders_point_at_labor_times(self, inputs, catalogs):
        df = _sched("Oct-26")
        df["FG_BASE"] = "BOSS25-010"
        df["ACC"] = ""
        st.session_state["_guide_imported_skus"] = ["NOPE-001"]
        step = app._guide_catalog_step(df, *catalogs, inputs)
        assert step["status"] == "info" and "still have zero labor" in step["fact"]
        assert _labels(step) == ["✏️ Set their labor times"]


# ---------------------------------------------------------------------------
# Step 4 — capacity
# ---------------------------------------------------------------------------
def _capacity(statuses: dict, required: dict) -> pd.DataFrame:
    idx = list(statuses)
    return pd.DataFrame({
        "overall_status": [statuses[s] for s in idx],
        "required_hc": [required.get(s, 0) for s in idx],
    }, index=idx)


class TestCapacityStep:
    def test_no_units(self, inputs):
        step = app._guide_capacity_step(pd.DataFrame(), inputs, None, pd.DataFrame())
        assert step["status"] == "info" and _labels(step) == []

    def test_over_and_unstaffed(self, inputs):
        cap = _capacity(
            {"PDI": "🔴 OVER", "ETO": "🔴 OVER", "Accessories KIT": "🔴 NO STATION", "QC": "🟢 OK"},
            {"PDI": 4, "ETO": 3, "Accessories KIT": 1, "QC": 2},
        )
        units = pd.DataFrame({"x": [1]})
        weekly = pd.DataFrame({"Wk 1": [0.9, 1.3], "Wk 2": [0.8, 0.7]}, index=["PDI", "ETO"])
        step = app._guide_capacity_step(cap, inputs, weekly, units)
        assert step["status"] == "error"
        assert "**2 station(s) over capacity** (PDI, ETO)" in step["fact"]
        assert "**1 not staffed here** (Accessories KIT)" in step["fact"]
        assert "need **10** people" in step["fact"]
        assert "busiest week **Wk 1** at 130%" in step["fact"]
        assert _labels(step) == ["👥 Edit headcount", "🔧 Recommendations", "💡 Level-load the month"]

    def test_all_clear_offers_only_level_loading(self, inputs):
        cap = _capacity({"PDI": "🟢 OK"}, {"PDI": 1})
        weekly = pd.DataFrame({"Wk 1": [0.5], "Wk 2": [0.6]}, index=["PDI"])
        step = app._guide_capacity_step(cap, inputs, weekly, pd.DataFrame({"x": [1]}))
        assert step["status"] == "ok" and step["fact"].startswith("Every station is within capacity")
        assert _labels(step) == ["💡 Level-load the month"]
        st.session_state["_proposed_schedule"] = pd.DataFrame({"a": [1]})
        step = app._guide_capacity_step(cap, inputs, weekly, pd.DataFrame({"x": [1]}))
        assert _labels(step) == [], "no second proposal while one is pending"

    def test_unsaved_crew_draft_is_named(self, inputs):
        cap = _capacity({"PDI": "🟢 OK"}, {"PDI": 1})
        st.session_state[f"_crew_draft_{LOCATION}"] = pd.DataFrame({"People": [3]}, index=["PDI"])
        step = app._guide_capacity_step(cap, inputs, None, pd.DataFrame({"x": [1]}))
        assert "crew edited, not saved" in step["fact"]
        assert _labels(step) == ["👥 Save crew (sidebar)"]


# ---------------------------------------------------------------------------
# Step 5 — save
# ---------------------------------------------------------------------------
class TestSaveStep:
    def test_hidden_unless_something_is_unsaved(self, inputs):
        assert app._guide_save_step(inputs) is None
        st.session_state["_last_upload_summary"] = {"rows": 1, "source": "saved", "name": "x"}
        assert app._guide_save_step(inputs) is None
        st.session_state["_schedule_edit_count"] = 1
        step = app._guide_save_step(inputs)
        assert step is not None and step["inline_save"] and "1 change(s)" in step["fact"]
        st.session_state["_last_upload_summary"] = {"rows": 1, "source": "upload", "name": "p.csv"}
        st.session_state["_schedule_edit_count"] = 0
        assert "lives in this browser only" in app._guide_save_step(inputs)["fact"]
        inputs["schedule_mode"] = app.SCHEDULE_MODE_MANUAL
        assert app._guide_save_step(inputs) is None


# ---------------------------------------------------------------------------
# Static shape
# ---------------------------------------------------------------------------
class TestShape:
    def test_main_draws_the_guide_and_no_banner_stack(self):
        src = inspect.getsource(app.main)
        assert "_render_plan_guide(" in src
        for gone in ("NOT in these numbers", "No PRODUCTION DAY in your schedule",
                     "carryover unit(s) — all scheduled", "➕ Import", "st.info(empty_banner_msg)"):
            assert gone not in src, gone
        assert "_render_tab_strip()" in src

    def test_sidebar_handlers_still_defer(self):
        for name in ("render_sidebar", "_uploaded_schedule_save", "_uploaded_schedule_load"):
            assert "st.rerun(" not in inspect.getsource(getattr(app, name)), name
        # the guide's inline save passes the main-panel rerun in explicitly
        assert "rerun=st.rerun" in inspect.getsource(app._render_guide_step)

    def test_the_sidebar_pill_is_one_line(self):
        src = inspect.getsource(app._render_upload_summary)
        assert "st.warning(" not in src
        assert "Auto-leveled" not in src
        assert "_schedule_detail_lines" in inspect.getsource(app._guide_schedule_step)


# ---------------------------------------------------------------------------
# In the app
# ---------------------------------------------------------------------------
def _button(at, prefix: str):
    for b in at.main.button:
        if (b.label or "").startswith(prefix):
            return b
    return None


def _strip(at):
    strips = [w for w in at.main.segmented_control if "Overview" in [str(o).strip() for o in w.options]]
    assert len(strips) == 1
    return strips[0]


def _headers(at) -> list:
    return [h.value for h in at.main.header] + [h.value for h in at.main.subheader]


@pytest.fixture
def at(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    return at


def test_the_guide_renders_once_with_its_steps(at):
    texts = [str(m.value) for m in at.main.markdown]
    assert sum("🧭 Plan guide" in t for t in texts) == 1
    rows = [str(x.value) for x in at.main.markdown] + [str(x.value) for x in at.main.warning] \
        + [str(x.value) for x in at.main.error] + [str(x.value) for x in at.main.info]
    for n, title in ((1, "Schedule"), (2, "Month"), (3, "Catalog"), (4, "Capacity")):
        assert any(f"**{n}. {title}**" in r for r in rows), (n, title)
    # the sidebar keeps one pill line and no warnings
    assert any(str(s.value).startswith("Loaded **") for s in at.sidebar.success)
    assert list(at.sidebar.warning) == []


def test_jump_to_the_recommendations_tab(at):
    btn = _button(at, "🔧 Recommendations")
    if btn is None:
        pytest.skip("the loaded plan has no capacity issue to act on")
    days = at.session_state["work_days_input"]
    btn.click().run()
    assert not at.exception
    assert _strip(at).value == app.TAB_RECOMMENDATIONS
    assert app.ACTIVE_TAB_SEED_KEY not in at.session_state, "seed consumed"
    assert at.session_state["work_days_input"] == days, "the sidebar was untouched"


def test_jump_to_the_machine_catalog_view(at):
    # Force a jump from a state where the button may not exist: call the
    # handler the button runs, through a fresh run.
    at.session_state[app.ACTIVE_TAB_SEED_KEY] = app.TAB_DATA_SETUP
    at.session_state["_data_setup_view_last"] = "🏭 Machine catalog"
    at.run()
    assert not at.exception
    assert _strip(at).value == app.TAB_DATA_SETUP
    picker = next(r for r in at.main.radio if r.label == "View")
    assert picker.value == "🏭 Machine catalog"


def test_edit_headcount_opens_the_crew_expander(at):
    btn = _button(at, "👥 Edit headcount")
    if btn is None:
        pytest.skip("every station is within capacity for the loaded plan")
    days = at.session_state["work_days_input"]
    btn.click().run()
    assert not at.exception
    assert at.session_state[f"crew_expander_{LOCATION}"] is True
    assert app.CREW_EXPANDER_SEED_KEY not in at.session_state
    assert at.session_state["work_days_input"] == days


def test_level_load_generates_a_proposal_under_the_guide(at):
    btn = _button(at, "💡 Level-load the month")
    if btn is None:
        pytest.skip("no dated schedule to level-load")
    btn.click().run()
    assert not at.exception
    assert "_proposed_schedule" in at.session_state
    assert _button(at, "✅ Accept proposal") is not None
    # The harness keeps the pre-rerun elements of a run that reran itself, so
    # assert what is *gone* on a plain run.
    at.run()
    assert not at.exception
    assert _button(at, "💡 Level-load the month") is None, "no second proposal while one is pending"
    _button(at, "🚫 Discard").click().run()
    assert not at.exception
    at.run()
    assert _button(at, "💡 Level-load the month") is not None
