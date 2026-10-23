"""The front page answers first and explains on request.

Walking the app on 2026-10-23, the first screen was mostly prose: up to five
full-width banners above the tab strip (units dropped, dates auto-leveled,
carryover / plan month, missing SKUs), six tall warning boxes in the narrow
sidebar under the "Loaded N rows" pill — several repeating the banners — and
an Overview of ten stacked sections, one of them a paragraph-long red box.

Under test here:

* everything the loader left out or filled in sits in ONE collapsed
  "Schedule notes — …" expander above the tabs, whose label carries each
  note's headline (so a short total is never silent);
* the sidebar's per-file notes are one collapsed expander of one-line
  bullets, not warning boxes;
* the Overview leads with a single row of four numbers;
* those upload notes don't follow the planner into ✏️ manual entry, where
  the uploaded file's summary is still in session state.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
app = pytest.importorskip("app")

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
LOCATION = "Henderson"
MANUAL_MODE = "✏️ Paste or type SKUs"

needs_saved_schedule = pytest.mark.skipif(
    not app._load_uploaded_schedules_dict(
        app._csv_mtime("uploaded_schedules.json")
    ).get(LOCATION),
    reason=f"no saved schedules for {LOCATION} in data/",
)


@pytest.fixture(scope="module")
def at(tmp_path_factory):
    import core.usage_tracker as usage_tracker
    log = tmp_path_factory.mktemp("usage") / "usage_log.jsonl"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(usage_tracker, "USAGE_LOG_PATH", log)
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.run()
        assert not at.exception
        yield at


def _notes_expanders(at) -> list:
    return [e for e in at.main.expander if " Schedule notes — " in str(e.label)]


def _dropped_units(at) -> int:
    rec = (at.session_state["_last_upload_summary"] or {}).get("recovered") or {}
    return int(sum((rec.get("unmatched") or {}).values()) + (rec.get("no_model") or 0))


@needs_saved_schedule
class TestUploadMode:
    def test_main_notes_are_one_expander_that_names_the_dropped_units(self, at):
        notes = _notes_expanders(at)
        assert len(notes) <= 1, [e.label for e in notes]
        dropped = _dropped_units(at)
        if dropped:
            assert len(notes) == 1
            assert f"{dropped} units not counted" in notes[0].label
            # The detail is inside it, not loose above the tab strip.
            assert any(
                f"{dropped} unit(s) in your file aren't counted" in w.value
                for w in notes[0].warning
            ), [w.value for w in notes[0].warning]

    def test_sidebar_file_notes_are_bullets_in_one_expander(self, at):
        assert list(at.sidebar.warning) == [], [w.value for w in at.sidebar.warning]
        boxes = [e for e in at.sidebar.expander if str(e.label).endswith("note(s) on this file")]
        assert len(boxes) <= 1
        if boxes:
            n = int(re.search(r"(\d+) note", boxes[0].label).group(1))
            bullets = [
                line for md in boxes[0].markdown for line in str(md.value).splitlines()
                if line.startswith("- ")
            ]
            assert len(bullets) == n
        if _dropped_units(at):
            assert boxes, "dropped units must be noted next to the pill"

    def test_overview_leads_with_one_row_of_four_numbers(self, at):
        assert at.session_state[app.ACTIVE_TAB_KEY] == app.TAB_OVERVIEW
        labels = [m.label for m in at.main.metric]
        if not labels:
            pytest.skip("no schedule expanded for the default facility")
        assert labels == [
            "Units planned", "Buildable units", "People you have", "People you need",
        ]


@needs_saved_schedule
def test_upload_notes_do_not_follow_into_manual_entry(tmp_path, monkeypatch):
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")
    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    if not _dropped_units(at):
        pytest.skip(f"the newest saved {LOCATION} schedule drops no units")
    radio = next(r for r in at.sidebar.radio if "build plan" in (r.label or ""))
    radio.set_value(MANUAL_MODE).run()
    assert not at.exception
    # The uploaded file's summary is still in session state …
    assert _dropped_units(at)
    # … but an empty manual grid has nothing to say about it.
    assert not any("units not counted" in e.label for e in _notes_expanders(at))
