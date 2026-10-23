"""Every edge case of the schedule has a way back.

- An upload the loader can't read (no usable header, or a CSV pandas can't
  parse) is a planner message, never a crash; its bytes never become the
  active schedule; the schedule that was active before it is kept aside and
  comes back when the file leaves the uploader or on "⏮ Back to the previous
  schedule"; 💾 Save has nothing to save meanwhile.
- A reshape (Sequencer ✅ Accept, 🗓 Apply, 🔀 Bulk move, 🧹 Strip, 📅 Move
  into plan month) goes through ``_commit_reshape``: the previous bytes are
  the undo step, the schedule keeps its source and name, the edit count
  climbs, and "⏮ Back to as loaded" returns to the bytes as loaded. A fresh
  load (📥, the newest save, the sample) starts the history over.
- One rule decides whether a "This week" / "Remaining" window applies.
"""
from __future__ import annotations

import inspect
import io
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

from core.data_loader import ScheduleColumnError, load_schedule  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
LOCATION = "Henderson"

HEADER = b"LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH\n"
GOOD_CSV = HEADER + b"HENDERSON,BOSS25-010,BOSS25-A016,5,May-26\n"
GOOD2_CSV = HEADER + b"HENDERSON,BOSS70-002,,2,May-26\nHENDERSON,BOSS25-010,,1,May-26\n"
GARBAGE = b'a,b\n1,2,3,4\n"unterminated'
EMPTY = b""


@pytest.fixture(autouse=True)
def _fresh_session_state(monkeypatch):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def _upload(name: str, data: bytes, file_id: str):
    """Stand-in for Streamlit's UploadedFile: the attributes app.py reads."""
    return SimpleNamespace(name=name, file_id=file_id, size=len(data), getvalue=lambda: data)


def _load(uploaded=None):
    return app._load_schedule_df(uploaded, location=LOCATION, target_month_hint=(2026, 5))


# ---------------------------------------------------------------------------
# 1. Unreadable CSVs are a message, not a crash
# ---------------------------------------------------------------------------
class TestReadScheduleTable:
    def test_ragged_rows_and_an_unterminated_quote(self):
        with pytest.raises(ScheduleColumnError) as exc:
            load_schedule(io.BytesIO(GARBAGE), location=LOCATION)
        assert "couldn't read this file as a CSV table" in str(exc.value)
        assert ".xlsx" in str(exc.value)

    def test_an_empty_file(self):
        with pytest.raises(ScheduleColumnError):
            load_schedule(io.BytesIO(EMPTY), location=LOCATION)

    def test_a_readable_file_still_loads(self):
        df = load_schedule(io.BytesIO(GOOD_CSV), location=LOCATION)
        assert len(df) == 1 and int(df["BUILD QTY"].iloc[0]) == 5


# ---------------------------------------------------------------------------
# 2. A failed upload keeps the way back
# ---------------------------------------------------------------------------
class TestFailedUpload:
    def test_garbage_never_becomes_the_active_schedule(self):
        _load(_upload("plan.csv", GOOD_CSV, "file-1"))
        assert st.session_state["_active_schedule_csv"] is GOOD_CSV

        bad = _upload("broken.csv", GARBAGE, "file-2")
        with pytest.raises(ScheduleColumnError):
            _load(bad)
        assert "_active_schedule_csv" not in st.session_state
        assert "couldn't read" in st.session_state["_active_schedule_error"]
        assert st.session_state["_schedule_before_error"]["csv"] is GOOD_CSV
        assert st.session_state["_schedule_before_error"]["name"] == "plan.csv"
        assert app._has_active_schedule(bad) is False, "💾 Save must be disabled"

        # Same file still in the uploader on the next rerun: still the banner,
        # never a silent revival.
        with pytest.raises(ScheduleColumnError) as again:
            _load(bad)
        assert "couldn't read" in str(again.value)

    def test_emptying_the_uploader_brings_the_previous_schedule_back(self):
        _load(_upload("plan.csv", GOOD_CSV, "file-1"))
        with pytest.raises(ScheduleColumnError):
            _load(_upload("broken.csv", GARBAGE, "file-2"))

        df = _load(None)   # the planner removed the file
        assert len(df) == 1
        assert st.session_state["_active_schedule_csv"] is GOOD_CSV
        assert st.session_state["_active_schedule_name"] == "plan.csv"
        assert "_active_schedule_error" not in st.session_state
        assert "_schedule_before_error" not in st.session_state
        assert st.session_state["_last_upload_summary"]["source"] == "upload"

    def test_back_to_previous_while_the_file_is_still_in_the_uploader(self):
        _load(_upload("plan.csv", GOOD_CSV, "file-1"))
        bad = _upload("broken.csv", GARBAGE, "file-2")
        with pytest.raises(ScheduleColumnError):
            _load(bad)

        assert app._restore_schedule_before_error() is True
        df = _load(bad)    # same (seen) file: step 3 serves the restored schedule
        assert len(df) == 1 and st.session_state["_active_schedule_csv"] is GOOD_CSV

    def test_with_nothing_before_the_error_the_facility_resolves_afresh(self):
        with pytest.raises(ScheduleColumnError):
            _load(_upload("broken.csv", GARBAGE, "file-1"))
        assert app._restore_schedule_before_error() is False
        df = _load(None)
        assert not df.empty
        assert "_active_schedule_error" not in st.session_state
        assert st.session_state["_last_upload_summary"]["source"] in ("saved", "bundled")

    def test_a_good_upload_after_a_bad_one_clears_everything(self):
        with pytest.raises(ScheduleColumnError):
            _load(_upload("broken.csv", GARBAGE, "file-1"))
        df = _load(_upload("plan.csv", GOOD_CSV, "file-2"))
        assert len(df) == 1
        assert "_active_schedule_error" not in st.session_state
        assert st.session_state["_schedule_original"]["csv"] is GOOD_CSV


# ---------------------------------------------------------------------------
# 3. Reshapes are undoable; a fresh load starts the history over
# ---------------------------------------------------------------------------
def _reshaped(df: pd.DataFrame, qty: int) -> pd.DataFrame:
    out = df[["LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY", "PRODUCTION MONTH"]].copy()
    out["BUILD QTY"] = qty
    return out


class TestReshapeHistory:
    def test_a_reshape_keeps_source_and_name_and_leaves_an_undo_step(self):
        up = _upload("plan.csv", GOOD_CSV, "file-1")
        df = _load(up)
        app._commit_reshape(_reshaped(df, 7), "Moved 1 row(s) into May 2026", event="x")
        assert st.session_state["_loaded_schedule_kind"] == "reshape"
        toast = st.session_state["_schedule_toast"]
        assert toast[0] == "success" and "Moved 1 row(s)" in toast[1]

        df2 = _load(up)
        assert int(df2["BUILD QTY"].iloc[0]) == 7
        assert st.session_state["_active_schedule_source"] == "upload", "not relabelled 'saved'"
        assert st.session_state["_active_schedule_name"] == "plan.csv"
        assert st.session_state["_last_upload_summary"]["source"] == "upload"
        assert app._schedule_edit_count() == 1
        assert st.session_state["_schedule_undo"]["csv"] is GOOD_CSV
        assert st.session_state["_schedule_undo"]["label"] == "Moved 1 row(s) into May 2026"
        assert st.session_state["_schedule_original"]["csv"] is GOOD_CSV

    def test_undo_then_restore(self):
        up = _upload("plan.csv", GOOD_CSV, "file-1")
        df = _load(up)
        app._commit_reshape(_reshaped(df, 7), "first")
        df = _load(up)
        first_bytes = st.session_state["_active_schedule_csv"]
        app._commit_reshape(_reshaped(df, 9), "second")
        df = _load(up)
        assert int(df["BUILD QTY"].iloc[0]) == 9 and app._schedule_edit_count() == 2

        assert app._undo_last_reshape() is True
        df = _load(up)
        assert int(df["BUILD QTY"].iloc[0]) == 7
        assert st.session_state["_active_schedule_csv"] == first_bytes
        assert app._schedule_edit_count() == 1
        assert "_schedule_undo" not in st.session_state     # single-level undo
        assert app._undo_last_reshape() is False

        assert app._restore_schedule_as_loaded() is True
        df = _load(up)
        assert int(df["BUILD QTY"].iloc[0]) == 5
        assert st.session_state["_active_schedule_csv"] is GOOD_CSV
        assert app._schedule_edit_count() == 0

    def test_a_fresh_load_starts_the_history_over(self, monkeypatch):
        up = _upload("plan.csv", GOOD_CSV, "file-1")
        df = _load(up)
        app._commit_reshape(_reshaped(df, 7), "edit")
        _load(up)
        assert app._schedule_edit_count() == 1

        monkeypatch.setattr(app, "get_uploaded_schedule",
                            lambda loc, name: {"csv_content": GOOD2_CSV.decode()})
        assert app._queue_saved_schedule_load(LOCATION, "Hen test") is True
        assert "_loaded_schedule_kind" not in st.session_state
        df = _load(up)                       # the upload is still in the widget
        assert len(df) == 2
        assert st.session_state["_active_schedule_csv"] == GOOD2_CSV
        assert st.session_state["_active_schedule_source"] == "saved"
        assert st.session_state["_active_schedule_name"] == "Hen test"
        assert app._schedule_edit_count() == 0
        assert "_schedule_undo" not in st.session_state
        assert st.session_state["_schedule_original"]["csv"] == GOOD2_CSV
        assert "Loaded saved schedule" in st.session_state["_schedule_toast"][1]

    def test_the_bundled_sample_is_a_chosen_schedule(self):
        assert app._queue_bundled_sample_load() is True
        df = _load(None)
        assert not df.empty
        assert st.session_state["_active_schedule_source"] == "bundled"
        assert st.session_state["_active_schedule_name"] == "May 2026 sample"
        assert "_active_schedule_auto_for" not in st.session_state
        assert st.session_state["_last_upload_summary"]["source"] == "bundled"
        # The next run serves it (step 3) — the newest saved schedule is NOT
        # picked again over the planner's choice.
        df2 = _load(None)
        assert st.session_state["_active_schedule_source"] == "bundled"
        assert len(df2) == len(df)

    def test_a_buffer_the_loader_cannot_read_leaves_the_active_schedule_alone(self):
        up = _upload("plan.csv", GOOD_CSV, "file-1")
        _load(up)
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(GARBAGE)
        with pytest.raises(ScheduleColumnError):
            _load(up)
        assert st.session_state["_active_schedule_csv"] is GOOD_CSV
        assert "_loaded_schedule_buffer" not in st.session_state

    def test_the_manual_resolver_drops_the_reshape_tags(self, monkeypatch):
        machine_df = app._project_facility_routing(
            app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
        )
        acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
        base = pd.DataFrame()
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(GOOD_CSV)
        st.session_state["_loaded_schedule_kind"] = "reshape"
        st.session_state["_loaded_schedule_label"] = "x"
        app._resolve_manual_schedule(base, LOCATION, machine_df, acc_df, plan_month=(2026, 5))
        for key in ("_loaded_schedule_buffer", "_loaded_schedule_kind", "_loaded_schedule_label"):
            assert key not in st.session_state

    def test_manual_mode_way_back(self):
        assert app._drop_manual_override() is False
        st.session_state["_manual_schedule_override"] = {"sig": "s", "csv": b"x"}
        assert app._drop_manual_override() is True
        assert "_manual_schedule_override" not in st.session_state
        assert "as typed" in st.session_state["_schedule_toast"][1]

    def test_every_reshape_handler_goes_through_commit_reshape(self):
        for fn in (app._render_sequencer_controls, app._apply_sequencer_edits,
                   app._render_bulk_move_form, app._render_suffix_cleanup_view, app.main):
            src = inspect.getsource(fn)
            assert '"_loaded_schedule_buffer"] = io.BytesIO' not in src, fn.__name__
        assert "_commit_reshape(" in inspect.getsource(app._render_bulk_move_form)
        assert "_commit_reshape(" in inspect.getsource(app.main)
        # The sidebar's 📥 Load still defers its rerun; the queue half is shared.
        src = inspect.getsource(app._uploaded_schedule_load)
        assert "st.rerun(" not in src and "_queue_saved_schedule_load" in src


# ---------------------------------------------------------------------------
# 4. One rule for the time window
# ---------------------------------------------------------------------------
class TestWindowApplies:
    def test_rules(self):
        now = app.now_local()
        this = (now.year, now.month)
        other = (now.year + 1, now.month)
        assert app._window_applies(this, []) is True
        assert app._window_applies(this, None) is True
        assert app._window_applies(this, [this, other]) is True
        assert app._window_applies(this, [other]) is False, "rows dated elsewhere"
        assert app._window_applies(other, []) is False, "plan month is not now"
        assert app._window_applies(None, [this]) is False

    def test_both_sides_use_it(self):
        assert "_window_applies(" in inspect.getsource(app.render_sidebar)
        assert "_window_applies(" in inspect.getsource(app.main)


# ---------------------------------------------------------------------------
# 5. In the app: 📅 Move, then ↩️ Undo
# ---------------------------------------------------------------------------
def _button(at, prefix: str):
    for b in at.main.button:
        if (b.label or "").startswith(prefix):
            return b
    return None


def test_accept_a_proposal_then_undo_in_the_app(tmp_path, monkeypatch):
    """Sequencer 💡 Generate → ✅ Accept is a reshape like any other: the
    toast survives the rerun, ↩️ Undo brings the schedule back byte for byte,
    and the edit count returns to zero."""
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")

    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    generate = _button(at, "💡 Generate proposal")
    if generate is None:
        pytest.skip("no schedule loaded, so there is nothing to level-load")
    before = at.session_state["_active_schedule_csv"]
    # AppTest's session_state has no .get()
    name_before = (at.session_state["_active_schedule_name"]
                   if "_active_schedule_name" in at.session_state else None)

    generate.click().run()
    assert not at.exception
    accept = _button(at, "✅ Accept proposal")
    assert accept is not None, [b.label for b in at.main.button]

    accept.click().run()
    assert not at.exception
    assert at.session_state["_schedule_edit_count"] == 1
    assert at.session_state["_active_schedule_csv"] != before
    name_after = (at.session_state["_active_schedule_name"]
                  if "_active_schedule_name" in at.session_state else None)
    assert name_after == name_before, "a reshape keeps the name"
    assert any("Accepted the level-loaded proposal" in str(s.value) for s in at.main.success), (
        "the toast survived the rerun"
    )
    undo = _button(at, "↩️ Undo")
    assert undo is not None and "Accepted" in undo.label
    assert _button(at, "⏮ Back to as loaded") is not None

    undo.click().run()
    assert not at.exception
    assert at.session_state["_schedule_edit_count"] == 0
    assert at.session_state["_active_schedule_csv"] == before
    assert _button(at, "↩️ Undo") is None
    assert any("Undid" in str(s.value) for s in at.main.success)
