"""The Overview answers first and explains on request.

Walking the app on 2026-10-23, the Overview stacked ten sections between
horizontal rules: two KPI rows of four under their own headings, a
paragraph-long red "Whole-mix cap" box plus a caption explaining the method,
a heatmap, fourteen station cards (one for a station the plan doesn't use)
and a battery chart. A planner read and scrolled before finding the verdict.
(The banner stack above the tabs went in the plan guide, tests/test_plan_guide.py.)

Under test here:

* the Overview leads with ONE row of four numbers;
* the station cards, the weekly heatmap and the battery chart sit behind
  small tabs, so one of them is on screen at a time;
* the plan guide's catalog step doesn't carry the last uploaded file's
  estimation-placeholder count into ✏️ manual entry, where that file's
  summary is still in session state.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"


# ---------------------------------------------------------------------------
# In the app
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def at(tmp_path_factory):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import core.usage_tracker as usage_tracker
    log = tmp_path_factory.mktemp("usage") / "usage_log.jsonl"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(usage_tracker, "USAGE_LOG_PATH", log)
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.run()
        assert not at.exception
        yield at


def _overview_shown(at) -> bool:
    return at.session_state[app.ACTIVE_TAB_KEY] == app.TAB_OVERVIEW and any(
        "Manufacturing" in str(m.value) for m in at.main.markdown
    )


def test_overview_leads_with_one_row_of_four_numbers(at):
    if not _overview_shown(at):
        pytest.skip("no schedule expanded for the default facility")
    assert [m.label for m in at.main.metric] == [
        "Units planned", "Buildable units", "People you have", "People you need",
    ]


def test_overview_visuals_are_one_at_a_time(at):
    if not _overview_shown(at):
        pytest.skip("no schedule expanded for the default facility")
    labels = [t.label for t in at.main.tabs]
    assert labels[0] == "📊 Stations" and labels[-1] == "🔋 Batteries", labels
    assert set(labels) <= {"📊 Stations", "📅 By week", "🔋 Batteries"}


# ---------------------------------------------------------------------------
# The guide's catalog step in manual mode (bare mode, like test_plan_guide)
# ---------------------------------------------------------------------------
@pytest.fixture
def _fresh_session_state(monkeypatch):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


@pytest.mark.usefixtures("_fresh_session_state")
@pytest.mark.parametrize("mode, noted", [
    (app.SCHEDULE_MODE_UPLOAD, True),
    (app.SCHEDULE_MODE_MANUAL, False),
])
def test_catalog_step_reads_the_upload_summary_in_upload_mode_only(mode, noted):
    machine_df = app._project_facility_routing(
        app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
    )
    acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
    df = pd.DataFrame({
        "LOCATION": ["HENDERSON"], "FG SKU ID": ["BOSS25-010"], "FG_BASE": ["BOSS25-010"],
        "FG ACCRY SKU ID": [""], "ACC": [""], "BUILD QTY": [1],
        "PRODUCTION MONTH": ["Oct-26"], "PRODUCTION DAY": [pd.NaT], "CARRYOVER": [False],
    })
    # The last upload put 4 units on estimation placeholders …
    st.session_state["_last_upload_summary"] = {"recovered": {"count": 4}}
    inputs = {"location": LOCATION, "schedule_mode": mode}
    step = app._guide_catalog_step(df, machine_df, acc_df, inputs)
    # … which describes this plan only while that upload is the plan.
    assert ("4 unit(s) run on an estimation placeholder" in step["fact"]) is noted
