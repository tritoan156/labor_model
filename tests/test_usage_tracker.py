"""``core.usage_tracker`` is local-only: it appends events to
``data/usage_log.jsonl`` on the container's disk and never talks to GitHub.

The file is git-ignored and untracked, so on a fresh container it does not
exist until the first flush -- every reader must treat that as an empty log.
"""
import inspect
from unittest import mock

import pytest

import core.usage_tracker as ut


class _FakeStreamlit:
    """Just enough of ``streamlit`` for the tracker: a dict-like session_state."""
    def __init__(self):
        self.session_state = {}


@pytest.fixture
def log_path(monkeypatch, tmp_path):
    path = tmp_path / "data" / "usage_log.jsonl"   # data/ does not exist yet
    monkeypatch.setattr(ut, "st", _FakeStreamlit())
    monkeypatch.setattr(ut, "USAGE_LOG_PATH", path)
    return path


def test_load_usage_log_tolerates_a_missing_file(log_path):
    assert not log_path.exists()
    assert ut.load_usage_log() == []


def test_flush_creates_the_file_and_clears_the_buffer(log_path):
    ut.track_event("catalog_save", facility="Henderson", file="machine_clean.csv",
                   empty="", none=None)
    assert ut.flush_buffer() == 0                # below FLUSH_THRESHOLD, not forced
    assert len(ut.get_buffer_snapshot()) == 1
    assert ut.flush_buffer(force=True) == 1
    assert log_path.exists()
    assert ut.get_buffer_snapshot() == []
    (event,) = ut.load_usage_log()
    assert event["type"] == "catalog_save"
    assert event["facility"] == "Henderson"
    assert event["file"] == "machine_clean.csv"
    assert "empty" not in event and "none" not in event   # blank payload dropped
    assert "uid" in event and "ts" in event


def test_flush_appends_to_an_existing_log(log_path):
    ut.track_event("session_start")
    ut.flush_buffer(force=True)
    ut.track_event("schedule_load")
    ut.flush_buffer(force=True)
    assert [e["type"] for e in ut.load_usage_log()] == ["session_start", "schedule_load"]
    assert log_path.read_text(encoding="utf-8").count("\n") == 2


def test_flush_keeps_the_buffer_when_the_write_fails(log_path, monkeypatch):
    monkeypatch.setattr(ut, "_write_local", mock.Mock(side_effect=OSError("read-only")))
    ut.track_event("crew_save")
    assert ut.flush_buffer(force=True) == 0
    assert len(ut.get_buffer_snapshot()) == 1     # still queued for the next try


def test_malformed_lines_are_skipped(log_path):
    log_path.parent.mkdir(parents=True)
    log_path.write_text('{"type":"a"}\nnot json\n\n[1,2]\n{"type":"b"}\n', encoding="utf-8")
    assert [e["type"] for e in ut.load_usage_log()] == ["a", "b"]


def test_no_github_plumbing_is_left():
    for gone in ("flush_to_github", "refresh_from_github", "_fetch_remote_log",
                 "GITHUB_FILE_PATH", "fetch_catalog_csv_from_github",
                 "save_catalog_to_github"):
        assert not hasattr(ut, gone), f"usage_tracker still exposes {gone}"
    assert "token" not in inspect.signature(ut.flush_buffer).parameters
