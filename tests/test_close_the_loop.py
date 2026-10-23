"""A save is in use the moment it lands, and nothing stale rides along.

Catalog CSV saves (the machine / accessory editors, the add-SKU and XXX
forms, ➕ Import N missing SKU(s), Reconciliation apply, the Excel import)
used to push to GitHub only: the session kept analysing the deployed copy
until the Streamlit Cloud rebuild, while the copy promised the values were
"in use now" and a 🔄 Refresh banner that never fires for one's own commit.
``core.catalog_storage.mirror_csv_locally`` now writes what was pushed into
this checkout's ``data/`` — the pattern every JSON store already used — so the
mtime-keyed loaders re-read it on the next run.

Also here: the missing-SKU list is computed from the schedule (so the ➕
Import button exists even when every FG SKU is unknown), a facility switch
drops a Sequencer proposal built for the other plant, ✏️ manual mode drops
the last upload's summary, and unsaved crew edits survive a facility
round-trip.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

import core.catalog_storage as catalog_storage  # noqa: E402
from core.data_loader import load_machine_labor, load_schedule  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
MACHINE_CSV = ROOT / "data" / "machine_clean.csv"
LOCATION = "Henderson"


@pytest.fixture(autouse=True)
def _fresh_session_state():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module ``core.catalog_storage`` uses."""

    def __init__(self, put_status: int = 200):
        self.put_status = put_status
        self.puts: list[dict] = []
        self.RequestException = RuntimeError

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(404)

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append(json)
        if self.put_status >= 400:
            return _Resp(self.put_status, text="conflict")
        return _Resp(200, {"commit": {"sha": "abc1234def"}, "content": {"sha": "sha-after"}})


# ---------------------------------------------------------------------------
# 1. The mirror itself
# ---------------------------------------------------------------------------
class TestMirrorCsvLocally:
    def test_a_successful_push_writes_the_same_bytes_under_data(self, tmp_path, monkeypatch):
        fake = _FakeRequests()
        monkeypatch.setattr(catalog_storage, "requests", fake)
        root = tmp_path / "repo"
        monkeypatch.setattr(catalog_storage, "REPO_ROOT", root)
        text = "SKU,Warehouse\r\nBOSS25-001,10.0\r\n"

        catalog_storage.save_catalog_to_github(text, "data/machine_clean.csv", "tok", "msg", sha="s")

        assert len(fake.puts) == 1
        assert (root / "data" / "machine_clean.csv").read_bytes() == text.encode("utf-8")

    def test_a_conflict_writes_nothing(self, tmp_path, monkeypatch):
        fake = _FakeRequests(put_status=409)
        monkeypatch.setattr(catalog_storage, "requests", fake)
        root = tmp_path / "repo"
        monkeypatch.setattr(catalog_storage, "REPO_ROOT", root)

        with pytest.raises(catalog_storage.GitHubConflict):
            catalog_storage.save_catalog_to_github("x,y\n", "data/machine_clean.csv", "tok", "m", sha="s")
        assert not (root / "data").exists()

    @pytest.mark.parametrize("path", [
        "data/facility_crew.json",   # JSON stores mirror themselves
        "../evil.csv",
        "data/../app.csv",
        "other/machine_clean.csv",
        "data/sub/machine_clean.csv",
    ])
    def test_only_data_csv_paths_are_mirrored(self, tmp_path, monkeypatch, path):
        root = tmp_path / "repo"
        monkeypatch.setattr(catalog_storage, "REPO_ROOT", root)
        assert catalog_storage.mirror_csv_locally(path, "a,b\n") is False
        assert not any(root.rglob("*")) if root.exists() else True

    def test_the_helper_never_raises_on_a_read_only_host(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        monkeypatch.setattr(catalog_storage, "REPO_ROOT", blocker)  # data/ can't be created
        assert catalog_storage.mirror_csv_locally("data/machine_clean.csv", "a,b\n") is False

    def test_the_test_suite_itself_is_pointed_at_a_temp_root(self):
        """conftest.py redirects REPO_ROOT for every test, so a stubbed save can
        never rewrite the tracked catalogs."""
        assert Path(catalog_storage.REPO_ROOT) != ROOT
        assert "repo" in str(catalog_storage.REPO_ROOT)


# ---------------------------------------------------------------------------
# 2. ➕ Import closes the loop: the next read sees the imported SKU
# ---------------------------------------------------------------------------
class TestImportClosesTheLoop:
    def test_an_imported_sku_is_in_the_catalog_the_app_reads_next(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        (root / "data").mkdir(parents=True)
        local = root / "data" / "machine_clean.csv"
        local.write_bytes(MACHINE_CSV.read_bytes())
        monkeypatch.setattr(catalog_storage, "REPO_ROOT", root)
        fake = _FakeRequests()
        monkeypatch.setattr(catalog_storage, "requests", fake)
        monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
        # The merge base is "the latest copy on GitHub"; here that is the file.
        monkeypatch.setattr(
            app, "_read_fresh_csv",
            lambda fp, tok: (pd.read_csv(local, dtype=str, keep_default_na=False), "sha-old"),
        )
        before = load_machine_labor(local)
        assert "SDG999-001" not in before.index

        added, skipped = app._import_missing_skus_to_catalog(
            "data/machine_clean.csv", "machine", ["SDG999-001"], "tok",
        )

        assert added == ["SDG999-001"] and skipped == []
        assert len(fake.puts) == 1, "the import pushed once"
        after = load_machine_labor(local)           # what the app reads next run
        assert "SDG999-001" in after.index
        assert float(after.loc["SDG999-001", "Warehouse"]) == 0.0
        assert len(after) == len(before) + 1

    def test_the_import_click_reruns_with_a_one_shot_message(self):
        """The handler stashes its outcome and reruns, so the warning above it
        is rebuilt from the catalogs as they are now and the message survives."""
        import inspect
        src = inspect.getsource(app.main)
        assert '"_import_toast"' in src
        assert "their units count now" in src
        assert "after the app redeploys" not in src


# ---------------------------------------------------------------------------
# 3. Missing SKUs from the schedule, not the unit table
# ---------------------------------------------------------------------------
def _schedule(rows: list[tuple]) -> pd.DataFrame:
    text = "LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH\n" + "\n".join(
        f"HENDERSON,{fg},{acc},{qty},May-26" for fg, acc, qty in rows
    )
    return load_schedule(io.BytesIO(text.encode()), location="Henderson")


class TestMissingCatalogSkus:
    @pytest.fixture(scope="class")
    def catalogs(self):
        machine_df = app._project_facility_routing(
            app._load_machine_df(app._csv_mtime("machine_clean.csv")), LOCATION,
        )
        acc_df = app._load_acc_df(app._csv_mtime("acc_clean.csv"))
        return machine_df, acc_df

    def test_every_fg_unknown_still_lists_them(self, catalogs):
        machine_df, acc_df = catalogs
        sched = _schedule([("NOPE-001", "NOPE-A001", 2), ("NOPE-002", "", 1)])
        units = app._expand_schedule(sched, machine_df, acc_df)
        assert units.empty, "expansion drops every unknown FG"
        fg, acc = app._missing_catalog_skus(sched, machine_df, acc_df)
        assert fg == ["NOPE-001", "NOPE-002"]
        assert acc == ["NOPE-A001"]

    def test_matches_the_unit_table_on_the_bundled_sample(self, catalogs):
        machine_df, acc_df = catalogs
        sched = load_schedule(location="Henderson", machine_skus=set(machine_df["SKU"]))
        units = app._expand_schedule(sched, machine_df, acc_df)
        fg, acc = app._missing_catalog_skus(sched, machine_df, acc_df)
        assert fg == list(units.attrs["skipped_fg"])
        assert set(units.attrs["unknown_acc"]) <= set(acc)

    def test_empty_schedule_is_empty(self, catalogs):
        machine_df, acc_df = catalogs
        assert app._missing_catalog_skus(pd.DataFrame(), machine_df, acc_df) == ([], [])
        assert app._missing_catalog_skus(None, machine_df, acc_df) == ([], [])

    def test_main_uses_it_for_the_import_block(self):
        import inspect
        src = inspect.getsource(app.main)
        assert "_missing_catalog_skus(schedule_df_full, machine_df, acc_df)" in src
        assert 'units.attrs.get("skipped_fg"' not in src


# ---------------------------------------------------------------------------
# 4. Stale state
# ---------------------------------------------------------------------------
class TestStaleState:
    def test_a_facility_switch_drops_the_proposal(self):
        st.session_state["_proposed_schedule"] = pd.DataFrame({"a": [1]})
        st.session_state["_proposed_schedule_overloads"] = [1]
        assert app._note_facility("Henderson") is False      # first sight
        assert "_proposed_schedule" in st.session_state
        assert app._note_facility("Henderson") is False      # same plant
        assert "_proposed_schedule" in st.session_state
        assert app._note_facility("Cypress") is True
        assert "_proposed_schedule" not in st.session_state
        assert "_proposed_schedule_overloads" not in st.session_state

    def test_manual_mode_drops_the_last_uploads_summary(self):
        st.session_state["_last_upload_summary"] = {"rows": 3, "recovered": {"no_model": 2}}
        app._note_schedule_mode(app.SCHEDULE_MODE_UPLOAD)
        assert "_last_upload_summary" in st.session_state
        app._note_schedule_mode(app.SCHEDULE_MODE_MANUAL)
        assert "_last_upload_summary" not in st.session_state
        # Back to upload mode: nothing to restore — the loader stashes a fresh one.
        app._note_schedule_mode(app.SCHEDULE_MODE_UPLOAD)
        assert "_last_upload_summary" not in st.session_state

    def test_the_sidebar_notes_the_facility(self):
        import inspect
        src = inspect.getsource(app.render_sidebar)
        assert "_note_facility(location)" in src


# ---------------------------------------------------------------------------
# 5. Unsaved crew edits survive a facility round-trip (AppTest)
# ---------------------------------------------------------------------------
def _crew_frame(at) -> pd.DataFrame:
    for frame in at.sidebar.dataframe:
        if "People" in list(frame.value.columns):
            return frame.value
    raise AssertionError("crew editor not rendered")


def _facility(at):
    return next(s for s in at.sidebar.selectbox if s.label == "Facility")


def test_crew_draft_survives_a_facility_round_trip(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import core.usage_tracker as usage_tracker
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")

    at = AppTest.from_file(str(APP_PATH), default_timeout=300)
    at.run()
    assert not at.exception
    saved = _crew_frame(at).copy()
    station = saved.index[0]
    new_people = int(saved.loc[station, "People"]) + 7

    # Type a number into the grid — the payload the browser sends.
    at.session_state[f"crew_editor_{LOCATION}"] = {
        "edited_rows": {"0": {"People": new_people}}, "added_rows": [], "deleted_rows": [],
    }
    at.run()
    assert not at.exception
    draft = at.session_state[f"_crew_draft_{LOCATION}"]
    assert int(draft.loc[station, "People"]) == new_people

    _facility(at).set_value("Cypress").run()
    assert not at.exception
    assert "People" in list(_crew_frame(at).columns)
    _facility(at).set_value(LOCATION).run()
    assert not at.exception

    shown = _crew_frame(at)
    assert int(shown.loc[station, "People"]) == new_people, "the unsaved edit was lost"
    assert int(saved.loc[station, "People"]) != new_people
