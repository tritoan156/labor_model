"""Regression tests for the schedule-editing flows fixed after the 2026-10-20
operational audit.

1. Every reshape action (Sequencer ✅ Accept, 🗓 Apply, 🔀 Bulk move, 🧹 Strip,
   📅 Move into plan month) hands its result over through the one-shot
   ``_loaded_schedule_buffer``. In ✏️ manual mode that buffer used to have no
   reader — "accepted — refreshing…" and nothing changed — and the leftover
   buffer then hijacked the next load after a switch to upload mode.
   ``_resolve_manual_schedule`` is the manual-mode half of the resolver and
   ``_note_schedule_mode`` clears cross-mode state on a mode switch.
2. 💾 Save schedule saves the ACTIVE schedule (every reshape applied), not the
   uploader's raw file, and works for a schedule loaded from the saved list.
3. Week filters key on (year, month, week) of PRODUCTION DAY, so a carryover
   row from an earlier month never merges into this month's same-numbered
   week; Bulk move's target weeks follow the plan month's calendar (4–6).
4. The stale-data banner is told about the saver's own commit.

Importing app.py executes the Streamlit script in bare mode (ScriptRunContext
warnings, but it loads — same approach as test_app_helpers and
test_excel_upload_app). The two AppTest cases drive the real sidebar.
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

from core import usage_tracker  # noqa: E402
from core.constants import week_of_month  # noqa: E402
from core.data_loader import build_manual_schedule  # noqa: E402

# Captured before any fixture patches it: the AppTest cases need the real
# st.rerun (the script runner handles it), and AppTest shares this module.
_REAL_RERUN = st.rerun

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"
PLAN_MONTH = (2026, 10)  # October 2026 starts on a Thursday → weeks 1..5
SEP_WK2 = dt.date(2026, 9, 8)   # week 2 of September 2026 …
OCT_WK2 = dt.date(2026, 10, 5)  # … and week 2 of October 2026: same number


@pytest.fixture(autouse=True)
def _fresh_session_state():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """No telemetry (data/usage_log.jsonl is tracked but must never be
    committed), no bare-mode reruns, no cache wipes between tests."""
    monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
    monkeypatch.setattr(app.st, "rerun", lambda *a, **k: None)
    monkeypatch.setattr(app.st.cache_data, "clear", lambda: None)


@pytest.fixture
def warnings(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(app.st, "warning", lambda msg, *a, **k: seen.append(str(msg)))
    return seen


@pytest.fixture(scope="module")
def catalogs():
    machine_df = app._project_facility_routing(
        app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
    )
    acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
    return machine_df, acc_df


def _two_skus(catalogs):
    skus = sorted(
        s for s in catalogs[0]["SKU"].astype(str) if s.strip() and "XXX" not in s
    )
    return skus[0], skus[1]


def _manual_base(catalogs, rows):
    """What main() builds from the sidebar grid before resolving."""
    machine_df, _acc = catalogs
    entries = pd.DataFrame(rows, columns=["FG SKU", "Accessory SKU", "Qty"])
    return build_manual_schedule(
        entries, location=LOCATION, machine_skus=set(machine_df["SKU"]),
    )


def _buffer_from(df: pd.DataFrame) -> io.BytesIO:
    """Exactly what Accept / Apply / Bulk move / Strip / Move write."""
    return io.BytesIO(df.to_csv(index=False).encode("utf-8"))


def _upload(name: str, data: bytes, file_id: str):
    """Stand-in for Streamlit's UploadedFile: the attributes app.py reads."""
    return SimpleNamespace(name=name, file_id=file_id, size=len(data), getvalue=lambda: data)


def _dated(rows):
    """Schedule-shaped frame from (sku, qty, date-or-None) rows, with the
    WEEK_OF_MONTH column the loader would attach."""
    dates = [r[2] for r in rows]
    return pd.DataFrame({
        "FG SKU ID": [r[0] for r in rows],
        "BUILD QTY": [r[1] for r in rows],
        "PRODUCTION DAY": pd.to_datetime([pd.Timestamp(d) if d else pd.NaT for d in dates]),
        "WEEK_OF_MONTH": pd.array(
            [week_of_month(d) if d else pd.NA for d in dates], dtype="Int64",
        ),
    })


# ---------------------------------------------------------------------------
# 1. Reshape actions in ✏️ manual mode
# ---------------------------------------------------------------------------
class TestManualModeResolver:
    def test_reshape_buffer_is_adopted_and_consumed(self, catalogs):
        machine_df, acc_df = catalogs
        a, b = _two_skus(catalogs)
        base = _manual_base(catalogs, [[a, "", 3], [b, "", 2]])
        reshaped = base.copy()
        reshaped["PRODUCTION DAY"] = [pd.Timestamp(2026, 10, 26), pd.Timestamp(2026, 10, 27)]
        st.session_state["_loaded_schedule_buffer"] = _buffer_from(reshaped)
        st.session_state["_loaded_schedule_source"] = "saved"

        df = app._resolve_manual_schedule(
            base, LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH,
        )

        # Consumed one-shot — nothing left to hijack a later upload-mode load.
        assert "_loaded_schedule_buffer" not in st.session_state
        assert "_loaded_schedule_source" not in st.session_state
        # The analysed schedule now carries the reshaped dates.
        assert sorted(df["PRODUCTION DAY"].dt.day.tolist()) == [26, 27]
        assert df["WEEK_OF_MONTH"].tolist() == [5, 5]
        assert set(df["FG_BASE"]) == {a, b}
        assert df["BUILD QTY"].sum() == 5
        override = st.session_state["_manual_schedule_override"]
        assert override["sig"] == app._manual_schedule_signature(base)

    def test_override_survives_reruns_until_the_grid_changes(self, catalogs):
        machine_df, acc_df = catalogs
        a, b = _two_skus(catalogs)
        rows = [[a, "", 3], [b, "", 2]]
        base = _manual_base(catalogs, rows)
        reshaped = base.copy()
        reshaped["PRODUCTION DAY"] = [pd.Timestamp(2026, 10, 26), pd.Timestamp(2026, 10, 27)]
        st.session_state["_loaded_schedule_buffer"] = _buffer_from(reshaped)
        app._resolve_manual_schedule(base, LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH)

        # Any later rerun (slider nudge, tab switch) rebuilds the same grid
        # content → the reshape rides along.
        again = app._resolve_manual_schedule(
            _manual_base(catalogs, rows), LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH,
        )
        assert sorted(again["PRODUCTION DAY"].dt.day.tolist()) == [26, 27]
        assert "_manual_schedule_override" in st.session_state

        # The planner changes a quantity → the reshape described a plan that
        # no longer exists, so the freshly built (auto-spread) grid wins.
        changed = app._resolve_manual_schedule(
            _manual_base(catalogs, [[a, "", 4], [b, "", 2]]),
            LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH,
        )
        assert "_manual_schedule_override" not in st.session_state
        assert changed["BUILD QTY"].tolist() == [4, 2]
        assert changed["PRODUCTION DAY"].notna().all()
        assert set(changed["PRODUCTION DAY"].dt.month) == {10}
        assert sorted(changed["PRODUCTION DAY"].dt.day.tolist()) != [26, 27]

    def test_row_splits_and_sku_rewrites_survive(self, catalogs):
        """A 🔀 Bulk move can split one grid row into two dated rows and a 🧹
        Strip rewrites SKU text — both must come back out of the resolver."""
        machine_df, acc_df = catalogs
        a, b = _two_skus(catalogs)
        base = _manual_base(catalogs, [[a, "", 3], [b, "", 2]])
        split = pd.concat([base, base.iloc[[0]]], ignore_index=True)
        split.loc[0, "BUILD QTY"] = 2
        split.loc[2, "BUILD QTY"] = 1
        split["PRODUCTION DAY"] = [
            pd.Timestamp(2026, 10, 5), pd.Timestamp(2026, 10, 6), pd.Timestamp(2026, 10, 19),
        ]
        st.session_state["_loaded_schedule_buffer"] = _buffer_from(split)

        df = app._resolve_manual_schedule(base, LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH)

        assert len(df) == 3
        assert df["BUILD QTY"].sum() == 5
        assert sorted(df["PRODUCTION DAY"].dt.day.tolist()) == [5, 6, 19]
        assert sorted(df["WEEK_OF_MONTH"].tolist()) == [2, 2, 4]

    def test_empty_grid_drops_a_pending_buffer_instead_of_leaking_it(self, catalogs):
        machine_df, acc_df = catalogs
        a, b = _two_skus(catalogs)
        st.session_state["_loaded_schedule_buffer"] = _buffer_from(
            _manual_base(catalogs, [[a, "", 1]]),
        )
        out = app._resolve_manual_schedule(
            pd.DataFrame(), LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH,
        )
        assert out.empty
        assert "_loaded_schedule_buffer" not in st.session_state
        assert "_manual_schedule_override" not in st.session_state

    def test_override_for_other_rows_is_discarded(self, catalogs):
        machine_df, acc_df = catalogs
        a, b = _two_skus(catalogs)
        st.session_state["_manual_schedule_override"] = {"sig": "someone-else's-grid", "csv": b"x"}
        base = _manual_base(catalogs, [[a, "", 3]])
        df = app._resolve_manual_schedule(base, LOCATION, machine_df, acc_df, plan_month=PLAN_MONTH)
        assert "_manual_schedule_override" not in st.session_state
        assert df["BUILD QTY"].tolist() == [3]
        assert df["PRODUCTION DAY"].notna().all()  # auto-spread still ran

    def test_signature_tracks_rows_not_blank_lines(self, catalogs):
        a, b = _two_skus(catalogs)
        same = _manual_base(catalogs, [[a, "", 3], [b, "", 2]])
        with_blank = _manual_base(catalogs, [[a, "", 3], ["", "", 0], [b, "", 2]])
        assert app._manual_schedule_signature(same) == app._manual_schedule_signature(with_blank)
        assert app._manual_schedule_signature(same) != app._manual_schedule_signature(
            _manual_base(catalogs, [[a, "", 3], [b, "", 3]]),
        )
        assert app._manual_schedule_signature(pd.DataFrame()) == ""

    def test_upload_mode_still_promotes_the_same_buffer(self):
        csv = (b"LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,PRODUCTION DAY\n"
               b"HENDERSON,BOSS25-010,,3,Oct-26,2026-10-26\n")
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(csv)
        df = app._load_schedule_df(None, location=LOCATION, target_month_hint=PLAN_MONTH)
        assert st.session_state["_active_schedule_csv"] == csv
        assert "_loaded_schedule_buffer" not in st.session_state
        assert df["PRODUCTION DAY"].dt.day.tolist() == [26]


class TestModeSwitch:
    def test_switch_discards_proposal_and_pending_buffer(self):
        # First run: nothing to compare against.
        assert app._note_schedule_mode(app.SCHEDULE_MODE_UPLOAD) is False
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(b"x")
        st.session_state["_loaded_schedule_source"] = "saved"
        st.session_state["_proposed_schedule"] = pd.DataFrame({"a": [1]})
        st.session_state["_proposed_schedule_overloads"] = [{"fg": "x"}]
        st.session_state["_active_schedule_csv"] = b"keep me"

        # Same mode again: everything untouched.
        assert app._note_schedule_mode(app.SCHEDULE_MODE_UPLOAD) is False
        assert "_loaded_schedule_buffer" in st.session_state

        # Switch: the proposal and the pending reshape were computed against
        # the other mode's schedule → gone. Upload mode's own active schedule
        # survives so a round trip comes back to it.
        assert app._note_schedule_mode(app.SCHEDULE_MODE_MANUAL) is True
        for key in app._CROSS_MODE_SCHEDULE_KEYS:
            assert key not in st.session_state
        assert st.session_state["_active_schedule_csv"] == b"keep me"
        assert st.session_state["_schedule_mode_seen"] == app.SCHEDULE_MODE_MANUAL


# ---------------------------------------------------------------------------
# AppTest: the real sidebar wiring, in ✏️ manual mode
# ---------------------------------------------------------------------------
def _mode_radio(at):
    for radio in at.sidebar.radio:
        if "build plan" in (radio.label or ""):
            return radio
    raise AssertionError("schedule-mode radio not found in the sidebar")


def _analysed_schedule(at) -> pd.DataFrame:
    """The 📋 Schedule view (Data & Setup's default sub-view) renders the
    frame that drives every analysis tab."""
    for frame in at.dataframe:
        cols = list(frame.value.columns)
        if "FG_RAW" in cols and "WEEK_OF_MONTH" in cols:
            return frame.value
    raise AssertionError("📋 Schedule view not rendered")


class TestManualModeInTheApp:
    @pytest.fixture
    def manual_app(self, catalogs, tmp_path, monkeypatch):
        AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
        # AppTest runs app.py in its own namespace but on this same streamlit
        # module: the Accept button's st.rerun() must really rerun, and the
        # script's own telemetry must not touch the tracked usage log.
        monkeypatch.setattr(app.st, "rerun", _REAL_RERUN)
        monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
        a, b = _two_skus(catalogs)
        at = AppTest.from_file(str(APP_PATH), default_timeout=120)
        at.session_state[f"manual_seed_{LOCATION}"] = pd.DataFrame({
            "FG SKU": [a, b], "Accessory SKU": ["", ""], "Qty": [3, 2],
        })
        at.run()
        _mode_radio(at).set_value(app.SCHEDULE_MODE_MANUAL).run()
        assert not at.exception
        return at

    def test_switching_modes_drops_a_pending_proposal(self, manual_app):
        at = manual_app
        at.session_state["_proposed_schedule"] = pd.DataFrame({"a": [1]})
        _mode_radio(at).set_value(app.SCHEDULE_MODE_UPLOAD).run()
        assert not at.exception
        assert "_proposed_schedule" not in at.session_state

    def test_accept_proposal_changes_the_analysed_schedule(self, manual_app):
        at = manual_app
        before = _analysed_schedule(at)
        assert len(before) == 2 and before["PRODUCTION DAY"].notna().all()
        # A proposal that pushes everything to the plan month's last working day.
        first = before["PRODUCTION DAY"].iloc[0]
        last_day = app._working_days_in_month(int(first.year), int(first.month))[-1]
        proposal = before.copy()
        proposal["PRODUCTION DAY"] = pd.Timestamp(last_day)
        assert not (before["PRODUCTION DAY"] == pd.Timestamp(last_day)).all()
        at.session_state["_proposed_schedule"] = proposal
        at.run()
        assert not at.exception

        accept = [b for b in at.button if "Accept proposal" in (b.label or "")]
        assert accept, "✅ Accept proposal not rendered in manual mode"
        accept[0].click().run()
        assert not at.exception

        after = _analysed_schedule(at)
        assert (after["PRODUCTION DAY"] == pd.Timestamp(last_day)).all()
        assert "_manual_schedule_override" in at.session_state
        assert "_loaded_schedule_buffer" not in at.session_state
        assert "_proposed_schedule" not in at.session_state


# ---------------------------------------------------------------------------
# 2. 💾 Save schedule saves the active plan
# ---------------------------------------------------------------------------
RAW = (b"LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,PRODUCTION DAY\n"
       b"HENDERSON,BOSS25-010,,3,Oct-26,\n")
EDITED = (b"LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,PRODUCTION DAY\n"
          b"HENDERSON,BOSS25-010,,3,Oct-26,2026-10-26\n")


@pytest.fixture
def github(monkeypatch):
    saved: dict = {}

    def fake_save(location, name, csv_text, token):
        saved.update(location=location, name=name, csv_text=csv_text, token=token)
        return {"commit": {"sha": "abc1234def", "html_url": "https://example.test/c"}}

    monkeypatch.setattr(app, "save_uploaded_schedule_to_github", fake_save)
    monkeypatch.setattr(app.st, "secrets", {"github_token": "tok"})
    return saved


class TestSaveSchedule:
    def test_saves_the_edited_active_schedule_not_the_uploader_file(self, github):
        up = _upload("plan.csv", RAW, "file-1")
        st.session_state["_seen_upload_sig"] = app._upload_sig(up)   # loader promoted it …
        st.session_state["_active_schedule_csv"] = EDITED             # … then a 🗓 Apply replaced it
        app._uploaded_schedule_save(LOCATION, "Plan A", up, [])
        assert github["csv_text"] == EDITED.decode()
        assert github["location"] == LOCATION and github["name"] == "Plan A"
        assert st.session_state[f"_uploaded_schedule_toast_{LOCATION}"][0] == "success"

    def test_saves_a_schedule_loaded_from_the_list_with_no_upload(self, github):
        st.session_state["_active_schedule_csv"] = EDITED
        app._uploaded_schedule_save(LOCATION, "Plan B", None, [])
        assert github["csv_text"] == EDITED.decode()

    def test_brand_new_upload_is_saved_before_the_loader_promotes_it(self, github):
        up = _upload("plan.csv", RAW, "file-2")
        st.session_state["_active_schedule_csv"] = b"old,schedule\n"
        app._uploaded_schedule_save(LOCATION, "Plan C", up, [])
        assert github["csv_text"] == RAW.decode()

    def test_pending_reshape_wins(self, github):
        st.session_state["_active_schedule_csv"] = RAW
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(EDITED)
        app._uploaded_schedule_save(LOCATION, "Plan D", None, [])
        assert github["csv_text"] == EDITED.decode()
        # Peeked, not consumed — the loader still promotes it on this run.
        assert "_loaded_schedule_buffer" in st.session_state

    def test_nothing_to_save_warns_and_does_not_push(self, github, warnings):
        app._uploaded_schedule_save(LOCATION, "Plan E", None, [])
        assert not github
        assert any("Nothing to save" in w for w in warnings)

    def test_overwrite_still_needs_a_second_click(self, github, warnings):
        st.session_state["_active_schedule_csv"] = EDITED
        pending = f"_uploaded_schedule_overwrite_pending_{LOCATION}"
        app._uploaded_schedule_save(LOCATION, "Plan A", None, ["Plan A"])
        assert not github
        assert st.session_state[pending] == "Plan A"
        assert any("already exists" in w for w in warnings)
        app._uploaded_schedule_save(LOCATION, "Plan A", None, ["Plan A"])
        assert github["name"] == "Plan A"
        assert pending not in st.session_state

    def test_button_enablement_follows_the_active_schedule(self):
        assert app._has_active_schedule(None) is False
        st.session_state["_active_schedule_csv"] = b"x"
        assert app._has_active_schedule(None) is True
        del st.session_state["_active_schedule_csv"]
        up = _upload("plan.csv", b"x", "file-3")
        assert app._has_active_schedule(up) is True        # fresh upload, not promoted yet
        st.session_state["_seen_upload_sig"] = app._upload_sig(up)
        assert app._has_active_schedule(up) is False       # promoted, then rejected: nothing analysable
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(b"y")
        assert app._has_active_schedule(None) is True


# ---------------------------------------------------------------------------
# 3. Week filters key on the month too
# ---------------------------------------------------------------------------
class TestWeekFilters:
    def test_the_precondition(self):
        assert week_of_month(SEP_WK2) == week_of_month(OCT_WK2) == 2

    def test_month_week_mask_keeps_same_numbered_weeks_of_two_months_apart(self):
        df = _dated([("A", 2, SEP_WK2), ("A", 3, OCT_WK2), ("B", 1, None)])
        assert df["WEEK_OF_MONTH"].tolist()[:2] == [2, 2]   # the merged key
        assert app._month_week_mask(df, 2026, 10, 2).tolist() == [False, True, False]
        assert app._month_week_mask(df, 2026, 9, 2).tolist() == [True, False, False]
        assert not app._month_week_mask(df, 2026, 10, 3).any()
        assert not app._month_week_mask(pd.DataFrame(), 2026, 10, 2).any()
        assert not app._month_week_mask(df.drop(columns=["PRODUCTION DAY"]), 2026, 10, 2).any()

    def test_month_weeks_follow_the_calendar_not_a_fixed_one_to_five(self):
        assert app._month_weeks(2026, 10) == [1, 2, 3, 4, 5]
        # Aug 1 2026 is a Saturday: week 1 has no working day, Mon Aug 31 is week 6.
        assert app._month_weeks(2026, 8) == [2, 3, 4, 5, 6]
        assert app._month_weeks(2026, 2) == [2, 3, 4, 5]

    def test_first_working_day_of_week(self):
        assert app._first_working_day_of_week(2026, 8, 6) == pd.Timestamp("2026-08-31")
        assert app._first_working_day_of_week(2026, 10, 1) == pd.Timestamp("2026-10-01")
        assert app._first_working_day_of_week(2026, 10, 4) == pd.Timestamp("2026-10-19")
        # Week 1 of August 2026 has no working day → the month's first one.
        assert app._first_working_day_of_week(2026, 8, 1) == pd.Timestamp("2026-08-03")

    def test_bulk_move_ignores_the_carryover_row_that_shares_the_week_number(self):
        df = _dated([("A", 2, SEP_WK2), ("A", 3, OCT_WK2)])
        new_df, moved = app._bulk_move_units(df, "A", 2, 4, 1, 2026, 10)
        assert moved == 1
        assert new_df.loc[0, "PRODUCTION DAY"] == pd.Timestamp(SEP_WK2)   # September row untouched
        assert new_df.loc[1, "BUILD QTY"] == 2
        assert new_df.loc[1, "PRODUCTION DAY"] == pd.Timestamp(OCT_WK2)
        split = new_df.iloc[2]
        assert split["BUILD QTY"] == 1
        assert split["PRODUCTION DAY"] == pd.Timestamp("2026-10-19")
        assert split["WEEK_OF_MONTH"] == 4
        # Asking for more than October's week 2 holds moves only October's
        # units — never September's.
        new_df, moved = app._bulk_move_units(df, "A", 2, 4, 5, 2026, 10)
        assert moved == 3
        assert new_df.loc[0, "PRODUCTION DAY"] == pd.Timestamp(SEP_WK2)
        assert new_df.loc[1, "PRODUCTION DAY"] == pd.Timestamp("2026-10-19")
        assert len(new_df) == 2

    def test_bulk_move_can_target_a_sixth_week(self):
        df = _dated([("A", 1, dt.date(2026, 8, 4))])
        new_df, moved = app._bulk_move_units(df, "A", 2, 6, 1, 2026, 8)
        assert moved == 1
        assert new_df.loc[0, "PRODUCTION DAY"] == pd.Timestamp("2026-08-31")
        assert new_df.loc[0, "WEEK_OF_MONTH"] == 6

    def test_bulk_move_nothing_eligible(self):
        df = _dated([("A", 2, SEP_WK2)])
        new_df, moved = app._bulk_move_units(df, "A", 2, 3, 1, 2026, 10)
        assert moved == 0 and new_df is df

    def test_weekly_heatmap_buckets_by_month_and_week(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "_expand_schedule", lambda slice_df, m, a: slice_df.copy())

        def fake_cap(units, crew, shift, days, *rest):
            calls.append((len(units), days))
            return pd.DataFrame({"labor_util_safe": [float(len(units))]}, index=["QC"])

        monkeypatch.setattr(app, "_build_capacity_table_cached", fake_cap)
        inputs = {"crew_config": None, "shift": 480, "safety": 0.85, "efficiency": 1.0}
        df = _dated([
            ("A", 1, SEP_WK2), ("A", 1, OCT_WK2), ("B", 1, dt.date(2026, 10, 6)),
            ("C", 1, dt.date(2026, 10, 1)),
        ])
        util = app._compute_weekly_utilization(df, None, None, inputs)
        assert list(util.columns) == ["Sep Wk 2", "Oct Wk 1", "Oct Wk 2"]
        assert util.loc["QC"].tolist() == [1.0, 1.0, 2.0]
        # Each bucket's capacity uses its own month's working-day count:
        # Sep 7–11 → 5 days, Oct 1–2 → 2 days, Oct 5–9 → 5 days.
        assert calls == [(1, 5), (1, 2), (2, 5)]
        # A single-month schedule keeps the short labels.
        util = app._compute_weekly_utilization(df.iloc[1:], None, None, inputs)
        assert list(util.columns) == ["Wk 1", "Wk 2"]
        assert app._compute_weekly_utilization(_dated([("A", 1, None)]), None, None, inputs) is None

    def test_calendar_slices_to_one_month(self):
        df = _dated([("A", 1, SEP_WK2), ("A", 1, OCT_WK2), ("B", 1, dt.date(2026, 10, 6))])
        sliced, ym = app._calendar_month_slice(df, None)
        assert ym == (2026, 10) and len(sliced) == 2
        sliced, ym = app._calendar_month_slice(df, (2026, 9))
        assert ym == (2026, 9) and len(sliced) == 1
        sliced, _ym = app._calendar_month_slice(df, (2026, 11))
        assert sliced.empty


# ---------------------------------------------------------------------------
# 4. Stale-data banner after the saver's own commit
# ---------------------------------------------------------------------------
def _fake_push(record: dict, new_sha: str = "new-sha"):
    def push(csv_text, file_path, token, message="", sha=None):
        record.update(csv_text=csv_text, file_path=file_path, sha=sha, message=message)
        return {"content": {"sha": new_sha}, "commit": {"sha": "c0ffee1234", "html_url": ""}}
    return push


class TestStaleBannerAfterOwnSave:
    FILE = "data/machine_clean.csv"

    def test_mark_catalog_saved_records_the_committed_sha(self):
        key = f"loaded_sha::{self.FILE}"
        app._mark_catalog_saved(self.FILE, {"content": {"sha": "new-sha"}, "commit": {"sha": "c"}})
        assert st.session_state[key] == "new-sha"
        # No content SHA in the payload → forget it; the next render re-records.
        app._mark_catalog_saved(self.FILE, {"commit": {"sha": "c"}})
        assert key not in st.session_state
        app._mark_catalog_saved(self.FILE, None)
        assert key not in st.session_state

    def test_banner_stays_quiet_after_the_savers_own_commit(self, monkeypatch, warnings):
        monkeypatch.setattr(app.st, "secrets", {"github_token": "tok"})
        monkeypatch.setattr(app, "_latest_sha_cached", lambda f, t: "new-sha")
        key = f"loaded_sha::{self.FILE}"
        st.session_state[key] = "old-sha"
        app._render_stale_data_banner(self.FILE)
        assert len(warnings) == 1 and "another user" in warnings[0]   # genuinely stale
        app._mark_catalog_saved(self.FILE, {"content": {"sha": "new-sha"}})
        app._render_stale_data_banner(self.FILE)
        assert len(warnings) == 1                                      # our own commit: no banner
        app._mark_catalog_saved(self.FILE, {})
        app._render_stale_data_banner(self.FILE)                       # re-records silently
        assert len(warnings) == 1 and st.session_state[key] == "new-sha"

    def test_import_missing_skus_marks_the_file_saved(self, monkeypatch):
        fresh = pd.DataFrame({"SKU": ["A"], "Description": ["x"], "Last Modified": [""]})
        monkeypatch.setattr(app, "_read_fresh_csv", lambda f, t: (fresh.copy(), "old-sha"))
        pushed: dict = {}
        monkeypatch.setattr(app, "save_catalog_to_github", _fake_push(pushed))
        added, skipped = app._import_missing_skus_to_catalog(self.FILE, "machine", ["NEW1", "A"], "tok")
        assert added == ["NEW1"] and skipped == ["A"]
        assert pushed["sha"] == "old-sha" and "NEW1" in pushed["csv_text"]
        assert st.session_state[f"loaded_sha::{self.FILE}"] == "new-sha"

    def test_save_catalog_csv_marks_the_file_saved(self, monkeypatch):
        monkeypatch.setattr(app.st, "secrets", {"github_token": "tok"})
        fresh = pd.DataFrame({"SKU": ["A", "B"], "Final": [10, 5], "Last Modified": ["", ""]})
        monkeypatch.setattr(app, "_read_fresh_csv", lambda f, t: (fresh.copy(), "old-sha"))
        pushed: dict = {}
        monkeypatch.setattr(app, "save_catalog_to_github", _fake_push(pushed))
        source_df = pd.DataFrame({"Final": [10, 5]}, index=pd.Index(["A", "B"], name="SKU"))
        edited = pd.DataFrame({"SKU": ["A", "B"], "Final": [20, 5]})
        app._save_catalog_csv(edited, source_df, ["Final"], self.FILE, "machine")
        assert pushed["sha"] == "old-sha" and "A,20" in pushed["csv_text"]
        assert st.session_state[f"loaded_sha::{self.FILE}"] == "new-sha"

    def test_recon_apply_marks_the_file_saved(self, monkeypatch):
        monkeypatch.setattr(app.st, "secrets", {"github_token": "tok"})
        fresh = pd.DataFrame({"SKU": ["ACC1"], "ComAcc": [10.0], "Last Modified": [""]})
        monkeypatch.setattr(app, "_read_fresh_csv", lambda f, t: (fresh.copy(), "old-sha"))
        pushed: dict = {}
        monkeypatch.setattr(app, "save_catalog_to_github", _fake_push(pushed))
        acc_df = pd.DataFrame({"ComAcc": [10.0]}, index=pd.Index(["ACC1"], name="SKU"))
        selected = pd.DataFrame({"SKU": ["ACC1"], "Side": ["Compressor"], "Sum of items": [25.0]})
        app._apply_recon_to_acc_csv(selected, acc_df)
        assert pushed["file_path"] == "data/acc_clean.csv" and pushed["sha"] == "old-sha"
        assert st.session_state["loaded_sha::data/acc_clean.csv"] == "new-sha"
