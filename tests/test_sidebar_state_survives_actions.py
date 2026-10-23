"""A sidebar action must not reset the settings around it — nor lie about
what loaded.

Streamlit drops the state of every widget a script run did not build, and a
run that ends in ``st.rerun()`` counts as a finished run. The sidebar's
📥 Load / 💾 Save / 🗑 Delete / ➕ Add handlers and the manual grid's renumber
used to rerun on the spot — before the Plan month, Time window, working-day
count, sliders and crew editor below them existed. One click on 📥 Load then
reset all of them. "Working days in the period" has no default of its own,
so it fell to its minimum of 1 while the browser still displayed 22, and
every capacity figure came out ~20× too high (ETO at 3145 %, "People you
need 1084"). The same early rerun wiped the tab strip, so the planner was
bounced back to Overview; and because the sidebar runs before main() loads
the schedule, the "✅ Loaded N rows" pill and the warnings that explain a
short total described the PREVIOUS schedule for one more click.

Under test: ``_defer_rerun`` (a sidebar rerun fires once the sidebar is
built), the working-days re-seed, the tab strip's mirror, and the pill drawn
by main() after the load. AppTest drives the real script — one run per step,
so related steps share one app instance.
"""
import inspect
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
app = pytest.importorskip("app")

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"
MANUAL_MODE = "✏️ Paste or type SKUs"
CAPACITY_HEADER = "📊 Capacity vs Demand"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def at(tmp_path, monkeypatch):
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    return at


def _widget(at, kind: str, label: str):
    for w in getattr(at.sidebar, kind):
        if (w.label or "") == label:
            return w
    raise AssertionError(f"sidebar {kind} {label!r} not rendered")


def _button(at, label: str):
    for b in at.sidebar.button:
        if (b.label or "") == label:
            return b
    raise AssertionError(f"sidebar button {label!r} not rendered")


def _strip(at):
    # AppTest lists a segmented control's options as the proto `content` (the
    # leading emoji becomes the icon); `.value` / `.set_value` use the label.
    strips = [
        w for w in at.main.segmented_control
        if "Overview" in [str(o).strip() for o in w.options]
    ]
    assert len(strips) == 1
    return strips[0]


def _headers(at) -> list:
    return [h.value for h in at.main.header] + [h.value for h in at.main.subheader]


def _pill(at) -> str:
    """The sidebar's "✅ Loaded N rows …" confirmation (AppTest reports an
    alert's text without its leading emoji, which becomes the icon)."""
    pills = [s.value for s in at.sidebar.success if str(s.value).startswith("Loaded **")]
    assert len(pills) == 1, pills
    return pills[0]


def _move_into_plan_month_button(at):
    """The main-panel "📅 Move N row(s) into <plan month>" button — present
    whenever the loaded schedule isn't dated in the plan month (true for the
    bundled May 2026 sample in any other month)."""
    for b in at.main.button:
        if (b.label or "").startswith("📅 Move"):
            return b
    pytest.skip("the loaded schedule is already on the plan month's calendar")


def _working_days() -> int:
    return len(app._working_days_in_month(*_plan_month_default()))


def _plan_month_default() -> tuple:
    now = app.now_local()
    return (now.year, now.month)


def _next_month() -> tuple:
    y, m = _plan_month_default()
    return (y + m // 12, m % 12 + 1)


def _saved_schedules() -> dict:
    return app._load_uploaded_schedules_dict(
        app._csv_mtime("uploaded_schedules.json")
    ).get(LOCATION, {}) or {}


needs_saved_schedule = pytest.mark.skipif(
    not _saved_schedules(), reason=f"no saved schedules for {LOCATION} in data/",
)


# ---------------------------------------------------------------------------
# 📥 Load keeps what the planner set
# ---------------------------------------------------------------------------
@needs_saved_schedule
def test_load_keeps_window_sliders_and_working_days(at):
    """The settings below the 📂 panel survive a 📥 Load — including the
    working-day count, which used to drop to 1 on the server while the
    browser kept showing the old number."""
    _widget(at, "radio", "Time window").set_value("This week").run()
    _widget(at, "slider", "Planning buffer (safety factor)").set_value(0.85).run()
    _widget(at, "slider", "New hire %").set_value(33).run()
    assert not at.exception
    assert at.session_state["work_days_input"] == 5  # one week
    assert "(bundled sample)" in _pill(at)

    _button(at, "📥 Load").click().run()
    assert not at.exception

    assert _widget(at, "radio", "Time window").value == "This week"
    assert _widget(at, "slider", "Planning buffer (safety factor)").value == 0.85
    assert _widget(at, "slider", "New hire %").value == 33
    assert _widget(at, "number_input", "Working days in the period").value == 5
    assert at.session_state["work_days_input"] == 5

    # The capacity picture is computed from those same inputs — no 20× blow-up.
    people = next(m.value for m in at.main.metric if m.label == "People you need")
    assert int(str(people).replace(",", "")) < 200, people


@needs_saved_schedule
def test_load_keeps_plan_month_and_active_tab(at):
    """A Plan month the planner picked, and the tab they were on, both
    survive a 📥 Load (the picker used to snap back to the current month;
    the strip used to bounce to Overview)."""
    nxt = _next_month()
    _widget(at, "selectbox", "Plan month").set_value(nxt).run()
    assert not at.exception
    expected_days = len(app._working_days_in_month(*nxt))
    assert at.session_state["work_days_input"] == expected_days

    _strip(at).set_value(app.TAB_CAPACITY).run()
    assert CAPACITY_HEADER in _headers(at)

    _button(at, "📥 Load").click().run()
    assert not at.exception

    assert _widget(at, "selectbox", "Plan month").value == nxt
    assert _widget(at, "number_input", "Working days in the period").value == expected_days
    assert CAPACITY_HEADER in _headers(at), "📥 Load bounced the planner to Overview"
    assert _strip(at).value == app.TAB_CAPACITY


@needs_saved_schedule
def test_pill_describes_the_schedule_on_screen_right_after_load(at):
    """The sidebar confirmation is drawn by main() after the load, so on the
    very run that 📥 Load lands it names the saved schedule — not the sample
    the planner just replaced."""
    assert "(bundled sample)" in _pill(at)
    summary_before = dict(at.session_state["_last_upload_summary"])

    _button(at, "📥 Load").click().run()
    assert not at.exception

    summary = at.session_state["_last_upload_summary"]
    assert summary["source"] == "saved"
    pill = _pill(at)
    assert "(saved schedule)" in pill
    assert f"**{summary['rows']}**" in pill
    assert summary["rows"] != summary_before["rows"] or "(bundled sample)" not in pill
    # The handler's own confirmation is replayed on the rerun instead of being
    # thrown away with the run that raised it.
    assert any("Loaded saved schedule" in str(s.value) for s in at.sidebar.success)


# ---------------------------------------------------------------------------
# the belt-and-braces guards
# ---------------------------------------------------------------------------
class TestWorkingDaysSeed:
    """``_sync_working_days`` in isolation (bare mode: no script run, but
    ``st.session_state`` works). The harness can't delete a widget's state
    from outside a run, so the wipe is modelled on the dict directly."""

    SIG = ((2026, 10), "Whole month", True)

    @pytest.fixture(autouse=True)
    def _clean(self):
        import streamlit as st
        for k in ("work_days_input", "_work_days_synced_sig"):
            st.session_state.pop(k, None)
        yield
        for k in ("work_days_input", "_work_days_synced_sig"):
            st.session_state.pop(k, None)

    def test_seeds_on_first_sight_and_on_a_new_signature(self):
        import streamlit as st
        app._sync_working_days(22, self.SIG)
        assert st.session_state["work_days_input"] == 22
        app._sync_working_days(5, ((2026, 10), "This week", True))
        assert st.session_state["work_days_input"] == 5

    def test_a_manual_edit_sticks_while_the_signature_holds(self):
        import streamlit as st
        app._sync_working_days(22, self.SIG)
        st.session_state["work_days_input"] = 18  # the planner typed 18
        app._sync_working_days(22, self.SIG)
        assert st.session_state["work_days_input"] == 18

    def test_reseeds_when_the_widget_state_is_gone(self):
        """A rerun that wiped the input's state used to leave it unseeded —
        and st.number_input then fell to min_value=1."""
        import streamlit as st
        app._sync_working_days(22, self.SIG)
        del st.session_state["work_days_input"]       # what the wipe does
        app._sync_working_days(22, self.SIG)           # same signature …
        assert st.session_state["work_days_input"] == 22  # … still re-seeded


def test_tab_strip_survives_a_rerun_raised_before_it_is_built(at):
    """📅 Move into plan month reruns from main() before the strip exists,
    which drops the strip's widget state; the mirror brings the planner back
    to the same tab instead of Overview."""
    _strip(at).set_value(app.TAB_CAPACITY).run()
    assert CAPACITY_HEADER in _headers(at)
    _move_into_plan_month_button(at).click().run()
    assert not at.exception
    assert _strip(at).value == app.TAB_CAPACITY, "the planner was bounced to Overview"
    assert CAPACITY_HEADER in _headers(at)
    # …and the settings below the sidebar's action buttons were untouched too.
    assert at.session_state["work_days_input"] == _working_days()


def test_manual_grid_renumber_keeps_plan_month(at):
    """Typing a SKU into the manual grid costs a rerun (the row gets its line
    number); that rerun must not reset the Plan month picked below it."""
    for radio in at.sidebar.radio:
        if "build plan" in (radio.label or ""):
            radio.set_value(MANUAL_MODE).run()
            break
    nxt = _next_month()
    _widget(at, "selectbox", "Plan month").set_value(nxt).run()
    assert not at.exception

    rev_key = f"manual_rev_{LOCATION}"
    rev = at.session_state[rev_key]
    editor_key = f"manual_entries_{LOCATION}_v{rev}"
    at.session_state[editor_key] = {
        "edited_rows": {"0": {"FG SKU": "BOSS25-010", "Qty": 2}},
        "added_rows": [], "deleted_rows": [],
    }
    at.run()
    assert not at.exception
    assert at.session_state[rev_key] == rev + 1, "the renumber rerun did not happen"
    assert _widget(at, "selectbox", "Plan month").value == nxt
    assert at.session_state["work_days_input"] == len(app._working_days_in_month(*nxt))


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------
SIDEBAR_HANDLERS = (
    "_uploaded_schedule_save", "_uploaded_schedule_load", "_uploaded_schedule_delete",
    "_render_uploaded_schedules_panel",
    "_scenario_save", "_scenario_load", "_scenario_delete", "_render_scenarios_panel",
    "_paste_commit", "_render_paste_import",
    "_render_sku_browser", "_render_fg_first", "_render_acc_first",
    "render_sidebar",
)


def test_sidebar_handlers_never_rerun_on_the_spot():
    """Everything that runs inside render_sidebar asks for its rerun through
    _defer_rerun; render_sidebar fires it last, after the Admin expander."""
    for name in SIDEBAR_HANDLERS:
        src = inspect.getsource(getattr(app, name))
        assert "st.rerun(" not in src, f"{name} calls st.rerun() directly"
    sidebar_src = inspect.getsource(app.render_sidebar)
    flush_at = sidebar_src.index("_flush_deferred_rerun()")
    assert flush_at > sidebar_src.index('"⚙️ Admin"')
    assert flush_at < sidebar_src.index("return {")
