"""The moment right after a schedule load must explain itself.

Walking the app as a planner on 2026-10-22: loading a saved August schedule
with the Plan month on October said "switch the Plan month in the sidebar to
match the data" but offered no button for it, while the one-click action on
offer was the more invasive "📅 Move N row(s) into October 2026". The 📂
picker listed saved schedules alphabetically, so Henderson defaulted to
"Hen August 07/16" although "Hen August 08/04" was newer; its caption said
"296 rows" (a line-break count, inflated by quoted multi-line cells) while
the pill said "Loaded 277 rows" — the 11 rows with no BUILD QTY were dropped
without a word, so the two numbers could not be reconciled. And the Plan
month picker offered only a rolling window of 2 months back to 12 forward,
so an older schedule could not be planned as its own month.

Under test here:

* the 📂 picker lists the newest save first, defaults to it, and captions
  the parsed row count (``count_csv_data_rows``), not the stored one;
* "📆 Plan <month> instead" sets the Plan month to the schedule's own
  dominant PRODUCTION MONTH — through a non-widget seed the picker consumes
  before the selectbox is built, since the sidebar has already built the
  widget by the time a main-body button runs — and the working-day count
  follows; the schedule's months are always on the picker's list; nothing
  switches without a click;
* the pill says "Loaded 277 of 288 rows" and accounts for the gap;
* a fresh session opens on the facility's newest saved schedule (the picker's
  default), named on the pill, instead of the bundled May 2026 sample — a
  facility with no saved schedules keeps the sample and says so; the pick is
  bound to its facility (a facility switch re-resolves) and ends the moment
  the planner uploads, loads or reshapes anything.

AppTest drives the real script. Its element tree is built from EVERY forward
message of a run, so after a click that ends in ``st.rerun()`` an element the
first pass drew can linger at a path the rerun did not overwrite — assert
positive facts after a click, and do a plain ``at.run()`` before asserting
that something is gone.
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
app = pytest.importorskip("app")

from core.data_loader import load_schedule  # noqa: E402
from core.uploaded_schedule_storage import (  # noqa: E402
    count_csv_data_rows, names_newest_first,
)

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"
MANUAL_MODE = "✏️ Paste or type SKUs"


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


def _sidebar_button(at, label: str):
    for b in at.sidebar.button:
        if (b.label or "") == label:
            return b
    raise AssertionError(f"sidebar button {label!r} not rendered")


def _main_button(at, prefix: str):
    """The first main-panel button whose label starts with ``prefix``, or None."""
    for b in at.main.button:
        if (b.label or "").startswith(prefix):
            return b
    return None


def _pill(at) -> str:
    """The "✅ Loaded N rows …" confirmation (AppTest drops the leading emoji)."""
    pills = [s.value for s in at.sidebar.success if str(s.value).startswith("Loaded **")]
    assert len(pills) == 1, pills
    return pills[0]


def _picker_caption(at) -> str:
    caps = [c.value for c in at.sidebar.caption if str(c.value).startswith("Last saved:")]
    assert len(caps) == 1, caps
    return caps[0]


def _month_caption(at) -> str:
    caps = [c.value for c in at.sidebar.caption if str(c.value).startswith("📅 **")]
    assert len(caps) == 1, caps
    return caps[0]


def _blocks_for(location: str) -> dict:
    return app._load_uploaded_schedules_dict(
        app._csv_mtime("uploaded_schedules.json")
    ).get(location, {}) or {}


def _saved_blocks() -> dict:
    return _blocks_for(LOCATION)


needs_saved_schedule = pytest.mark.skipif(
    not _saved_blocks(), reason=f"no saved schedules for {LOCATION} in data/",
)


def _now_month() -> tuple:
    now = app.now_local()
    return (now.year, now.month)


def _next_month(ym: tuple) -> tuple:
    y, m = ym
    return (y + m // 12, m % 12 + 1)


def _load_saved(at, name: str):
    """Pick ``name`` in the 📂 panel and press 📥 Load (two runs)."""
    _widget(at, "selectbox", "Saved schedule").set_value(name).run()
    assert not at.exception
    _sidebar_button(at, "📥 Load").click().run()
    assert not at.exception
    assert at.session_state["_last_upload_summary"]["source"] == "saved"


def _ensure_plan_month_differs_from(at, ym: tuple) -> tuple:
    """Make sure the picker is NOT on ``ym`` (it is, when the test runs in
    the month the schedule is for); returns the plan month in force."""
    plan = at.session_state[app.PLAN_MONTH_KEY]
    if tuple(plan) == tuple(ym):
        plan = _next_month(ym)
        _widget(at, "selectbox", "Plan month").set_value(plan).run()
        assert not at.exception
    return tuple(plan)


# ---------------------------------------------------------------------------
# 📂 picker: newest first, honest row count
# ---------------------------------------------------------------------------
@needs_saved_schedule
def test_picker_lists_the_newest_save_first_and_defaults_to_it(at):
    blocks = _saved_blocks()
    picker = _widget(at, "selectbox", "Saved schedule")
    assert picker.options == names_newest_first(blocks)
    assert picker.value == picker.options[0]
    saved_at = [str(blocks[n].get("saved_at", "")) for n in picker.options]
    assert saved_at == sorted(saved_at, reverse=True), picker.options
    # …so the default is the most recent save, not the alphabetical first.
    assert picker.value == max(blocks, key=lambda n: str(blocks[n].get("saved_at", "")))


@needs_saved_schedule
def test_picker_caption_counts_the_rows_the_csv_really_has(at):
    blocks = _saved_blocks()
    picker = _widget(at, "selectbox", "Saved schedule")
    parsed = count_csv_data_rows(blocks[picker.value].get("csv_content", ""))
    assert f"**{parsed}** rows" in _picker_caption(at)

    # A block whose stored count is the old line-break count shows the parsed
    # number, so the caption can be reconciled with the pill after a 📥 Load.
    stale = [
        n for n, b in blocks.items()
        if b.get("rows") != count_csv_data_rows(b.get("csv_content", ""))
    ]
    if not stale:
        pytest.skip("every saved block already stores the parsed row count")
    name = stale[0]
    picker.set_value(name).run()
    assert not at.exception
    parsed = count_csv_data_rows(blocks[name]["csv_content"])
    caption = _picker_caption(at)
    assert f"**{parsed}** rows" in caption
    assert f"**{blocks[name]['rows']}** rows" not in caption


# ---------------------------------------------------------------------------
# the pill: "Loaded 277 of 288 rows" and why
# ---------------------------------------------------------------------------
@needs_saved_schedule
def test_pill_says_loaded_n_of_m_and_accounts_for_rows_with_no_qty(at):
    blocks = _saved_blocks()
    name = names_newest_first(blocks)[0]
    csv_text = blocks[name]["csv_content"]
    _load_saved(at, name)

    summary = at.session_state["_last_upload_summary"]
    pill = _pill(at)
    source_rows = count_csv_data_rows(csv_text)
    assert summary["source_rows"] == source_rows
    qty = pd.to_numeric(pd.read_csv(io.StringIO(csv_text))["BUILD QTY"], errors="coerce")
    no_qty = int((qty.isna() | (qty <= 0)).sum())
    assert summary["no_qty_rows"] == no_qty
    if summary["rows"] != source_rows:
        assert f"Loaded **{summary['rows']}** of **{source_rows}** rows" in pill
    else:
        assert f"Loaded **{summary['rows']}** rows" in pill
    if no_qty:
        assert f"{no_qty} row(s) had no BUILD QTY and were skipped; 0 units lost" in pill
    else:
        assert "had no BUILD QTY" not in pill


# ---------------------------------------------------------------------------
# first load: the facility's newest saved schedule, not the sample
# ---------------------------------------------------------------------------
@needs_saved_schedule
def test_fresh_session_opens_on_the_newest_saved_schedule(at):
    blocks = _saved_blocks()
    newest = names_newest_first(blocks)[0]
    summary = at.session_state["_last_upload_summary"]
    assert summary["source"] == "saved"
    assert summary["name"] == newest
    assert summary["auto_loaded"] is True
    assert summary["rows"] > 0
    assert at.session_state["_active_schedule_auto_for"] == LOCATION
    assert at.session_state["_active_schedule_name"] == newest
    pill = _pill(at)
    assert "(saved schedule)" in pill and "(bundled sample)" not in pill
    assert f"**{newest}**" in pill and "loaded automatically" in pill
    assert f"Loaded **{summary['rows']}**" in pill
    # The picker agrees on which save is the newest, and the sidebar says
    # that it loads first.
    assert _widget(at, "selectbox", "Saved schedule").value == newest
    assert any(
        "newest schedule saved for this facility loads first" in str(c.value)
        for c in at.sidebar.caption
    )
    # The Plan month is left alone: an August file opened in October gets the
    # "📆 Plan August 2026 instead" offer, never a silent switch.
    assert tuple(at.session_state[app.PLAN_MONTH_KEY]) == _now_month()
    sched_ym = summary["schedule_month"]
    if sched_ym and tuple(sched_ym) != _now_month():
        assert _main_button(at, "📆 Plan ") is not None


@pytest.fixture
def at_without_saved_schedules(tmp_path, monkeypatch):
    """A fresh app for a facility with no saved schedules: point the storage
    module at a file that doesn't exist and drop the cached dict — the
    st.cache_data store is process-wide, so the real dict a previous test
    cached would otherwise be served (and this test's empty one afterwards)."""
    import streamlit as st
    import core.usage_tracker as usage_tracker
    import core.uploaded_schedule_storage as us
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
    monkeypatch.setattr(us, "SCHEDULES_PATH", tmp_path / "no_such_uploaded_schedules.json")
    st.cache_data.clear()
    try:
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.run()
        assert not at.exception
        yield at
    finally:
        st.cache_data.clear()


def test_a_facility_with_no_saved_schedules_keeps_the_bundled_sample(at_without_saved_schedules):
    at = at_without_saved_schedules
    assert at.session_state["_last_upload_summary"]["source"] == "bundled"
    pill = _pill(at)
    assert pill.startswith("Loaded **193** rows from")   # 193 of 193: no "of"
    assert "(bundled sample)" in pill
    assert "bundled May 2026 sample" in pill
    assert " of **" not in pill and "had no BUILD QTY" not in pill
    assert "_active_schedule_csv" not in at.session_state
    assert "_active_schedule_auto_for" not in at.session_state
    assert "Saved schedule" not in [w.label for w in at.sidebar.selectbox]


@needs_saved_schedule
def test_facility_switch_re_resolves_an_automatic_pick_but_keeps_a_planners_choice(at):
    other = next((loc for loc in app.LOCATIONS if loc != LOCATION and _blocks_for(loc)), None)
    if other is None:
        pytest.skip("a second facility with saved schedules is needed")
    _widget(at, "selectbox", "Facility").set_value(other).run()
    assert not at.exception
    other_newest = names_newest_first(_blocks_for(other))[0]
    summary = at.session_state["_last_upload_summary"]
    assert summary["name"] == other_newest and summary["auto_loaded"] is True
    assert at.session_state["_active_schedule_auto_for"] == other
    assert f"from **{other}**" in _pill(at)

    # An explicit 📥 Load is the planner's choice: it ends the automatic pick …
    _sidebar_button(at, "📥 Load").click().run()
    assert not at.exception
    assert at.session_state["_last_upload_summary"]["auto_loaded"] is False
    assert "_active_schedule_auto_for" not in at.session_state
    assert "loaded automatically" not in _pill(at)

    # … and a facility switch no longer replaces it.
    _widget(at, "selectbox", "Facility").set_value(LOCATION).run()
    assert not at.exception
    assert at.session_state["_active_schedule_name"] == other_newest
    assert "_active_schedule_auto_for" not in at.session_state
    assert f"**{other_newest}**" in _pill(at)


@needs_saved_schedule
def test_mode_switch_and_back_does_not_re_trigger_the_first_load(at):
    names = names_newest_first(_saved_blocks())
    if len(names) < 2:
        pytest.skip("two saved schedules are needed")
    _load_saved(at, names[1])
    assert at.session_state["_active_schedule_name"] == names[1]

    radio = next(r for r in at.sidebar.radio if "build plan" in (r.label or ""))
    radio.set_value(MANUAL_MODE).run()
    assert not at.exception
    radio = next(r for r in at.sidebar.radio if "build plan" in (r.label or ""))
    radio.set_value(app.SCHEDULE_MODE_UPLOAD).run()
    assert not at.exception

    summary = at.session_state["_last_upload_summary"]
    assert summary["name"] == names[1] and summary["auto_loaded"] is False
    assert at.session_state["_active_schedule_name"] == names[1]
    assert "_active_schedule_auto_for" not in at.session_state
    assert f"**{names[1]}**" in _pill(at)


# ---------------------------------------------------------------------------
# 📆 Plan <month> instead
# ---------------------------------------------------------------------------
@needs_saved_schedule
def test_switch_button_plans_the_schedules_own_month_and_working_days_follow(at):
    """Load → the schedule's month differs from the Plan month → one click
    sets the picker to that month, the working-day count follows, and the
    offer disappears. Nothing switches before the click."""
    blocks = _saved_blocks()
    name = names_newest_first(blocks)[0]
    _load_saved(at, name)
    sched_ym = at.session_state["_last_upload_summary"]["schedule_month"]
    assert sched_ym is not None, "the saved schedule has no parseable PRODUCTION MONTH"
    sched_ym = tuple(sched_ym)
    plan_before = _ensure_plan_month_differs_from(at, sched_ym)
    label = app._month_label(sched_ym)

    # No auto-switch: the picker still says what the planner chose …
    assert tuple(_widget(at, "selectbox", "Plan month").value) == plan_before
    assert f"_(CSV says {label})_" in _pill(at)
    # … and the one-click answer is on offer.
    button = _main_button(at, "📆 Plan ")
    assert button is not None, [b.label for b in at.main.button]
    assert button.label == f"📆 Plan {label} instead"

    button.click().run()
    assert not at.exception

    assert tuple(_widget(at, "selectbox", "Plan month").value) == sched_ym
    assert tuple(at.session_state[app.PLAN_MONTH_KEY]) == sched_ym
    assert app.PLAN_MONTH_SEED_KEY not in at.session_state, "seed was not consumed"
    expected_days = len(app._working_days_in_month(*sched_ym))
    assert at.session_state["work_days_input"] == expected_days
    assert _widget(at, "number_input", "Working days in the period").value == expected_days
    assert _month_caption(at).startswith(f"📅 **{label}** — {expected_days} working days")
    pill = _pill(at)
    assert f"· month **{label}**" in pill
    assert "CSV says" not in pill  # the file's month IS the plan month now

    # A fresh run (clean element tree): the offer is gone, the month sticks.
    at.run()
    assert not at.exception
    assert _main_button(at, "📆 Plan ") is None
    assert tuple(_widget(at, "selectbox", "Plan month").value) == sched_ym
    assert at.session_state["work_days_input"] == expected_days


def _block_with_dates_off_the_calendar(blocks: dict, plan_ym: tuple):
    """A saved block carrying PRODUCTION DAY values outside ``plan_ym`` —
    the case the "📅 Move N row(s)" warning fires for — or None."""
    for name in names_newest_first(blocks):
        df = load_schedule(
            io.BytesIO(blocks[name]["csv_content"].encode("utf-8")),
            location=LOCATION.upper(),
        )
        if "PRODUCTION DAY" not in df.columns:
            continue
        days = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce").dropna()
        off = [d for d in days if (d.year, d.month) != tuple(plan_ym)]
        if off and app._schedule_dominant_month(df) not in (None, tuple(plan_ym)):
            return name
    return None


@needs_saved_schedule
def test_switch_button_sits_beside_move_and_makes_an_older_month_selectable(at):
    """With rows frozen in another month the Move warning fires; the switch
    button is offered next to Move. The schedule's month may be older than
    the picker's rolling window — it is on the list anyway, gets selected,
    and survives later reruns."""
    blocks = _saved_blocks()
    plan_ym = tuple(at.session_state[app.PLAN_MONTH_KEY])
    name = _block_with_dates_off_the_calendar(blocks, plan_ym)
    if name is None:
        pytest.skip(f"no saved {LOCATION} schedule has dates outside {plan_ym}")
    _load_saved(at, name)
    sched_ym = tuple(at.session_state["_last_upload_summary"]["schedule_month"])
    plan_ym = _ensure_plan_month_differs_from(at, sched_ym)
    label = app._month_label(sched_ym)

    move = _main_button(at, "📅 Move ")
    switch = _main_button(at, "📆 Plan ")
    assert move is not None and switch is not None, [b.label for b in at.main.button]
    assert switch.label == f"📆 Plan {label} instead"
    warning = next(w.value for w in at.main.warning if "calendar" in str(w.value))
    assert f"plan **{label}** instead" in warning

    # The sidebar builds the picker before main() loads the schedule, so the
    # schedule's own months join the list from the NEXT run on (the button
    # does not need them there: the seed is always on the list).
    at.run()
    assert not at.exception
    window = app._plan_month_options(app.now_local())
    picker = _widget(at, "selectbox", "Plan month")
    assert label in picker.options, picker.options
    for ym in at.session_state[app.SCHEDULE_MONTHS_KEY]:
        assert app._month_label(ym) in picker.options

    switch = _main_button(at, "📆 Plan ")
    switch.click().run()
    assert not at.exception
    picker = _widget(at, "selectbox", "Plan month")
    assert tuple(picker.value) == sched_ym
    assert label in picker.options
    assert at.session_state["work_days_input"] == len(app._working_days_in_month(*sched_ym))
    if sched_ym not in window:
        # Older than the rolling window: planned as its own month regardless.
        assert len(picker.options) > len(window)

    # Unrelated reruns keep the selection — it never drops off the list.
    _widget(at, "radio", "Time window").set_value("This week").run()
    assert not at.exception
    assert tuple(_widget(at, "selectbox", "Plan month").value) == sched_ym
    at.run()
    assert _main_button(at, "📆 Plan ") is None  # the plan month IS the file's month
    assert tuple(_widget(at, "selectbox", "Plan month").value) == sched_ym


# ---------------------------------------------------------------------------
# the helpers, in bare mode
# ---------------------------------------------------------------------------
class TestPlanMonthOptions:
    NOW = dt.datetime(2026, 10, 23, 9, 0)

    def test_default_window_is_two_back_twelve_forward(self):
        opts = app._plan_month_options(self.NOW)
        assert opts[0] == (2026, 8)
        assert (2026, 10) in opts
        assert opts[-1] == (2027, 10)
        assert len(opts) == 15
        assert opts == sorted(opts)

    def test_extra_months_are_added_in_order_without_duplicates(self):
        opts = app._plan_month_options(self.NOW, extra=[(2026, 6), (2026, 10), (2025, 2)])
        assert opts[:3] == [(2025, 2), (2026, 6), (2026, 8)]
        assert opts.count((2026, 10)) == 1
        assert len(opts) == 17

    def test_lists_and_strings_are_normalised_and_junk_is_ignored(self):
        opts = app._plan_month_options(
            self.NOW, extra=[[2026, 6], ("2024", "12"), None, (2026, 13), "June", (2026,)],
        )
        assert (2026, 6) in opts and (2024, 12) in opts
        assert all(isinstance(y, int) and isinstance(m, int) for y, m in opts)
        assert len(opts) == 17

    def test_year_rollover(self):
        opts = app._plan_month_options(dt.datetime(2026, 1, 15))
        assert opts[0] == (2025, 11)
        assert opts[-1] == (2027, 1)


class TestScheduleDominantMonth:
    @staticmethod
    def _df(months, qtys, days=None):
        df = pd.DataFrame({"PRODUCTION MONTH": months, "BUILD QTY": qtys})
        if days is not None:
            df["PRODUCTION DAY"] = pd.to_datetime(days)
        return df

    def test_units_not_rows_decide(self):
        df = self._df(["Jul-26", "Jul-26", "Jul-26", "Aug-26"], [1, 1, 1, 300])
        assert app._schedule_month_units(df) == {(2026, 7): 3, (2026, 8): 300}
        assert app._schedule_dominant_month(df) == (2026, 8)

    def test_production_day_is_ignored(self):
        # Right after a load every blank day has been auto-spread into the
        # Plan month; the file's own month is PRODUCTION MONTH.
        df = self._df(["Aug-26", "Aug-26"], [5, 5], days=["2026-10-05", "2026-10-06"])
        assert app._schedule_dominant_month(df) == (2026, 8)

    def test_carryover_format_counts_too(self):
        df = self._df(["26-Aug", "Aug-26"], [10, 1])
        assert app._schedule_month_units(df) == {(2026, 8): 11}

    def test_tie_goes_to_the_later_month(self):
        df = self._df(["26-Apr", "May-26"], [20, 20])
        assert app._schedule_dominant_month(df) == (2026, 5)

    def test_unparseable_months_are_skipped(self):
        df = self._df(["Manual", "", "NEED SN/QA", "Jun-26"], [9, 9, 9, 1])
        assert app._schedule_month_units(df) == {(2026, 6): 1}
        assert app._schedule_dominant_month(df) == (2026, 6)

    def test_nothing_parseable_or_empty_is_none(self):
        assert app._schedule_dominant_month(self._df(["Manual"], [3])) is None
        assert app._schedule_dominant_month(pd.DataFrame()) is None
        assert app._schedule_dominant_month(None) is None
        assert app._schedule_month_units(pd.DataFrame({"BUILD QTY": [1]})) == {}


def test_month_label():
    assert app._month_label((2026, 8)) == "August 2026"
    assert app._month_label([2027, 1]) == "January 2027"


def test_switch_button_lives_in_main_and_the_picker_takes_no_index():
    """The seed pattern: the main-body button writes PLAN_MONTH_SEED_KEY and
    reruns; render_sidebar consumes it before the selectbox is built, so the
    widget needs no `index=` (its state is always seeded) and the sidebar
    still never reruns on the spot."""
    import inspect
    sidebar_src = inspect.getsource(app.render_sidebar)
    assert "PLAN_MONTH_SEED_KEY" in sidebar_src
    assert "st.rerun(" not in sidebar_src
    picker = sidebar_src[sidebar_src.index('"Plan month",'):]
    picker = picker[:picker.index("help=")]
    assert "index=" not in picker
    assert "key=PLAN_MONTH_KEY" in picker
    main_src = inspect.getsource(app.main)
    assert "PLAN_MONTH_SEED_KEY" in main_src
    assert 'key="plan_schedule_month_btn"' in main_src
