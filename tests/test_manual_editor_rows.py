"""Row-numbering checks that drive the real app through Streamlit's AppTest.

The unit tests in `test_paste_import.py` cover `_manual_add_row_numbers` in
isolation. This file exists because the bug that kept coming back lived
*outside* that function: rows pasted straight into the grid are held in the
data_editor's own widget state, which Streamlit replays over whatever frame
the script passes in — so they rendered unnumbered no matter how the input
was numbered. Only a full app run exercises that replay.

One app run per step makes these slower than the rest of the suite, so the
whole flow lives in a single test.
"""
from pathlib import Path

import pandas as pd
import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"
MANUAL_MODE = "✏️ Paste or type SKUs"


def _mode_radio(at):
    for radio in at.sidebar.radio:
        if "build plan" in (radio.label or ""):
            return radio
    raise AssertionError("schedule-mode radio not found in the sidebar")


def _editor_frame(at) -> pd.DataFrame:
    """The manual-entry editor — the only sidebar table carrying a `#`."""
    for frame in at.sidebar.dataframe:
        if "#" in list(frame.value.columns):
            return frame.value
    raise AssertionError("manual-entry editor not rendered")


def _edit_grid(at, *, edited_rows=None, added_rows=None, deleted_rows=None):
    """Apply an edit the way the browser does — through the editor's own state.

    AppTest can't click a canvas grid, so the edit is written straight to the
    data_editor's widget state: exactly the payload the frontend sends for a
    paste, a typed cell, or a deleted row.

    Caveat worth knowing before adding steps: AppTest honors only the *first*
    write to a given widget key. Each step here therefore has to land on a
    fresh key, which holds as long as every step but the last bumps the
    revision.
    """
    key = f"manual_entries_{LOCATION}_v{at.session_state[f'manual_rev_{LOCATION}']}"
    at.session_state[key] = {
        "edited_rows": edited_rows or {},
        "added_rows": added_rows or [],
        "deleted_rows": deleted_rows or [],
    }
    at.run()


def test_rows_pasted_into_the_grid_are_numbered(tmp_path, monkeypatch):
    # Keep the app's telemetry out of the repo's data/ folder.
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")

    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    _mode_radio(at).set_value(MANUAL_MODE).run()
    assert not at.exception

    seed_key = f"manual_seed_{LOCATION}"
    rev_key = f"manual_rev_{LOCATION}"

    # Three committed rows plus the spare blanks a paste-commit leaves behind.
    at.session_state[seed_key] = pd.DataFrame({
        "FG SKU": ["BOSS25-010", "BOSS70-002", "BOSS125-001", "", ""],
        "Accessory SKU": ["", "", "", "", ""],
        "Qty": [1, 1, 1, 0, 0],
    })
    at.run()
    assert _editor_frame(at)["#"].tolist() == ["1", "2", "3", "", ""]

    # Paste three rows straight into the grid. Streamlit records a client-side
    # paste as `added_rows` on the editor's widget state — this is that state,
    # verbatim, including the columns the paste never touches.
    rev = at.session_state[rev_key]
    _edit_grid(at, added_rows=[{"FG SKU": "PDS185EZ-002", "Qty": 1}] * 3)
    assert not at.exception

    shown = _editor_frame(at)
    # Pasted rows continue the count; the blanks in the middle stay blank and
    # don't consume a number.
    assert shown["#"].tolist() == ["1", "2", "3", "", "", "4", "5", "6"]
    assert shown["FG SKU"].tolist()[5:] == ["PDS185EZ-002"] * 3

    # The rows were folded into the seed, and the widget was retired so the
    # stale added_rows can't replay (and duplicate) on the next run.
    assert at.session_state[rev_key] == rev + 1
    assert len(at.session_state[seed_key]) == 8
    assert "#" not in at.session_state[seed_key].columns

    # A further run must be stable — no repeated rows, no renumbering churn.
    at.run()
    assert _editor_frame(at)["#"].tolist() == ["1", "2", "3", "", "", "4", "5", "6"]

    # Typing a SKU into one of the blank rows numbers it straight away — the
    # same widget-state replay, reached by hand instead of by paste.
    _edit_grid(at, edited_rows={"3": {"FG SKU": "BOSS125-003", "Qty": 2}})
    assert not at.exception
    assert _editor_frame(at)["#"].tolist() == ["1", "2", "3", "4", "", "5", "6", "7"]

    # Deleting a row closes the gap rather than leaving a hole in the count.
    _edit_grid(at, deleted_rows=[1])
    assert not at.exception
    shown = _editor_frame(at)
    assert shown["#"].tolist() == ["1", "2", "3", "", "4", "5", "6"]
    assert "BOSS70-002" not in shown["FG SKU"].tolist()

    # Editing a SKU that's already numbered changes nothing about the count,
    # so it must not cost the extra rerun the other edits need.
    rev = at.session_state[rev_key]
    _edit_grid(at, edited_rows={"0": {"FG SKU": "BOSS70-012"}})
    assert at.session_state[rev_key] == rev
    assert _editor_frame(at)["#"].tolist() == ["1", "2", "3", "", "4", "5", "6"]
