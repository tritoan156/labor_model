"""Every path that adds a SKU to the machine catalog seeds its routing.

Four entry points append rows to ``data/machine_clean.csv``: the ➕ Add-SKU
form, the ➕ XXX-placeholder form (it builds its row the same way; the
accessory catalog it writes has no routing columns, so there the seed is a
no-op), the Overview's **➕ Import N missing SKU(s)** button, and the Excel
import's new rows (``_append_catalog_rows`` under ``_save_catalog_csv``). Each
used to write the 15 per-facility routing cells blank, so the SKU loaded as
the default team everywhere. These tests push a row through each path with
GitHub stubbed and check the CSV that would be committed, and that each
preview shows the planner what the SKU got.

The two forms are driven through AppTest (the real Data & Setup views); the
other paths are called directly, as ``tests/test_excel_catalog_import.py``
does.
"""
from __future__ import annotations

import inspect
import io
import re
from pathlib import Path

import pandas as pd
import pytest

import core.catalog_storage as catalog_storage
import core.usage_tracker as usage_tracker
from core.sku_routing import (
    RoutingSeeder, all_routing_columns, routing_columns_in, seed_facility_routing,
)

app = pytest.importorskip("app")

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
MACHINE_CSV = ROOT / "data" / "machine_clean.csv"
ACC_CSV = ROOT / "data" / "acc_clean.csv"
COLS = all_routing_columns()


def _real_machine() -> pd.DataFrame:
    return pd.read_csv(MACHINE_CSV, dtype=str, keep_default_na=False)


def _pushed_frame(csv_text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)


def _assert_fully_routed(row: pd.Series, sku: str) -> None:
    for col in COLS:
        assert str(row[col]).strip(), f"{sku}: {col} is blank"


def _expected(sku: str, desc: str = "") -> dict:
    """What the seeder says the real catalog gives this SKU."""
    return seed_facility_routing(_real_machine(), sku, desc).values


# ---------------------------------------------------------------------------
# 1. Excel import — new rows materialised by _append_catalog_rows
# ---------------------------------------------------------------------------
class TestExcelImportNewRows:
    def test_blank_routing_cells_are_seeded(self):
        out, added, skipped = app._append_catalog_rows(
            _real_machine(), [{"SKU": "PDS999-001", "Description": "NEW PDS", "Warehouse": 7.0}],
        )
        assert added == ["PDS999-001"] and skipped == []
        row = out[out["SKU"] == "PDS999-001"].iloc[0]
        _assert_fully_routed(row, "PDS999-001")
        assert {c: row[c] for c in COLS} == _expected("PDS999-001", "NEW PDS")
        assert row["Final Station HND"] == "Undercarriage"
        assert row["Undercarriage Station HND"] == "ComAcc"
        assert row["Warehouse"] == 7.0 and row["Last Modified"] == app.today_local_str()

    def test_a_routing_value_on_the_sheet_wins_over_the_seed(self):
        out, _, _ = app._append_catalog_rows(
            _real_machine(),
            [{"SKU": "PDS999-001", "Final Station HND": "Final", "AccKIT Station CYP": ""}],
        )
        row = out[out["SKU"] == "PDS999-001"].iloc[0]
        assert row["Final Station HND"] == "Final"             # the sheet said so
        assert row["AccKIT Station CYP"] == "ComAcc"           # blank on the sheet → seeded
        _assert_fully_routed(row, "PDS999-001")

    def test_existing_rows_are_untouched(self):
        fresh = _real_machine()
        blank_before = int((fresh[COLS] == "").sum().sum())
        out, _, _ = app._append_catalog_rows(fresh, [{"SKU": "SDG999-001"}])
        assert int((out.iloc[:len(fresh)][COLS] == "").sum().sum()) == blank_before
        assert out.iloc[:len(fresh)].equals(fresh)

    def test_the_save_path_commits_the_seeded_row(self, monkeypatch):
        calls = []

        def fake_save(csv_text, file_path, token, message, sha=None):
            calls.append(csv_text)
            return {"commit": {"sha": "abc1234def", "html_url": "u"}}

        monkeypatch.setattr(app, "_read_fresh_csv", lambda fp, tok: (_real_machine(), "sha"))
        monkeypatch.setattr(app, "save_catalog_to_github", fake_save)
        monkeypatch.setattr(app, "_track_and_flush", lambda *a, **k: None)
        monkeypatch.setattr(app.st, "secrets", {"github_token": "tok"})
        result = app._save_catalog_csv(
            edited=None, source_df=pd.DataFrame(), editable_cols=["Warehouse"],
            file_path="data/machine_clean.csv", label="machine",
            text_cols=["Description", *COLS], diffs=[],
            additions=[{"SKU": "SDG999-001", "Description": "NEW GEN"}], via="Excel import",
        )
        assert result == "abc1234" and len(calls) == 1
        pushed = _pushed_frame(calls[0])
        row = pushed[pushed["SKU"] == "SDG999-001"].iloc[0]
        _assert_fully_routed(row, "SDG999-001")
        assert row["AccKIT Station CYP"] == "GenAcc" and row["Wire Station CYP"] == "GenAcc"

    def test_the_import_preview_shows_the_seeded_routing(self):
        src = inspect.getsource(app._render_catalog_import_section)
        assert "_routing_seed_preview(spec.file_path, diff.new_rows)" in src


# ---------------------------------------------------------------------------
# 2. Overview — ➕ Import N missing SKU(s)
# ---------------------------------------------------------------------------
class TestImportMissingSkus:
    @pytest.fixture
    def github(self, monkeypatch):
        pushed = {}

        def fake_save(csv_text, file_path, token, message, sha=None):
            pushed.update(csv_text=csv_text, message=message, sha=sha)
            return {"commit": {"sha": "abc1234def"}, "content": {"sha": "new-sha"}}

        monkeypatch.setattr(app, "_read_fresh_csv", lambda fp, tok: (_real_machine(), "old-sha"))
        monkeypatch.setattr(app, "save_catalog_to_github", fake_save)
        return pushed

    def test_every_imported_sku_is_fully_routed(self, github):
        skus = ["PDS999-001", "SDG999-001", "BOSS25-099", "10999-ZZ"]
        added, skipped = app._import_missing_skus_to_catalog(
            "data/machine_clean.csv", "machine", skus + ["BOSS25-001"], "tok",
        )
        assert added == skus and skipped == ["BOSS25-001"]
        pushed = _pushed_frame(github["csv_text"]).set_index("SKU")
        for sku in skus:
            _assert_fully_routed(pushed.loc[sku], sku)
            assert {c: pushed.at[sku, c] for c in COLS} == _expected(sku)
        assert pushed.at["PDS999-001", "Final Station HND"] == "Undercarriage"
        assert pushed.at["SDG999-001", "PDI Station CYP"] == "GenAcc"
        assert pushed.at["BOSS25-099", "Final Station HND"] == "Final"
        # Labor stays blank — this is still the zero-labor quick import.
        assert pushed.at["PDS999-001", "Warehouse"] == ""

    def test_the_accessory_catalog_is_a_no_op(self, monkeypatch):
        acc = pd.read_csv(ACC_CSV, dtype=str, keep_default_na=False)
        pushed = {}
        monkeypatch.setattr(app, "_read_fresh_csv", lambda fp, tok: (acc.copy(), "s"))
        monkeypatch.setattr(
            app, "save_catalog_to_github",
            lambda csv_text, *a, **k: pushed.update(csv_text=csv_text) or {"commit": {}},
        )
        added, _ = app._import_missing_skus_to_catalog("data/acc_clean.csv", "accessory", ["BOSS25-A999"], "tok")
        assert added == ["BOSS25-A999"]
        out = _pushed_frame(pushed["csv_text"])
        assert list(out.columns) == list(acc.columns) and not routing_columns_in(out.columns)

    def test_the_overview_shows_the_routing_before_the_click(self):
        src = inspect.getsource(app.main)
        assert re.search(r'_routing_seed_preview\(\s*"data/machine_clean\.csv",\s*\[\{"SKU": s\} for s in _skipped_fg\]', src)


# ---------------------------------------------------------------------------
# The preview helpers both multi-SKU paths render
# ---------------------------------------------------------------------------
class TestPreviewHelpers:
    def test_seed_preview_from_the_csv_on_disk(self):
        table = app._routing_seed_preview(
            "data/machine_clean.csv",
            [{"SKU": "PDS999-001"}, {"SKU": "SDG999-001", "Description": "GEN"},
             {"SKU": "   "}, None],
        )
        assert list(table.columns) == ["SKU", "Family", "Class", "Seeded from", "HND", "SPB", "CYP"]
        assert list(table["SKU"]) == ["PDS999-001", "SDG999-001"]
        assert "Final→Undercarriage" in table.at[0, "HND"] and "Undercarriage→ComAcc" in table.at[0, "HND"]
        assert table.at[1, "CYP"] == "AccKIT→GenAcc · PDI→GenAcc · Wire→GenAcc"
        assert table.at[0, "Seeded from"].endswith("PDS Standard row(s)")

    def test_a_routing_value_the_row_already_has_is_shown_as_the_sheets(self):
        table = app._routing_seed_preview(
            "data/machine_clean.csv", [{"SKU": "PDS999-001", "Final Station HND": "Final"}],
        )
        assert "Final→" not in table.at[0, "HND"]
        assert table.at[0, "Seeded from"].startswith("the sheet / ")

    def test_nothing_to_preview(self):
        assert app._routing_seed_preview("data/acc_clean.csv", [{"SKU": "BOSS25-A999"}]) is None
        assert app._routing_seed_preview("data/machine_clean.csv", []) is None
        assert app._routing_seed_preview("data/machine_clean.csv", [{"SKU": ""}]) is None

    def test_raw_csv_keeps_blanks_as_text(self):
        raw = app._raw_catalog_csv_cached("data/machine_clean.csv", 0.0)
        assert (raw[COLS] == "").any().any()
        assert raw["Warehouse"].dtype == object
        assert app._raw_catalog_csv_cached("data/not_there.csv", 0.0).empty

    def test_render_seeded_routing_shows_stations_by_plant(self, monkeypatch):
        shown = {}
        monkeypatch.setattr(app.st, "caption", lambda msg, *a, **k: shown.setdefault("caption", str(msg)))
        monkeypatch.setattr(app.st, "dataframe", lambda df, *a, **k: shown.setdefault("frame", df))
        app._render_seeded_routing(seed_facility_routing(_real_machine(), "PDS999-001"))
        assert "54 PDS Standard row(s)" in shown["caption"]
        assert list(shown["frame"].columns) == ["Station", "HND", "SPB", "CYP"]
        assert shown["frame"].set_index("Station").at["Undercarriage", "HND"] == "ComAcc"

    def test_render_seeded_routing_is_silent_without_routing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app.st, "caption", lambda *a, **k: calls.append("caption"))
        monkeypatch.setattr(app.st, "dataframe", lambda *a, **k: calls.append("frame"))
        app._render_seeded_routing(None)
        app._render_seeded_routing(seed_facility_routing(pd.DataFrame({"SKU": ["A"]}), "B"))
        assert calls == []


# ---------------------------------------------------------------------------
# 3 + 4. The two forms, driven through the real Data & Setup views
# ---------------------------------------------------------------------------
def _run(at):
    """``at.run()``, seeding any session key the bare harness complains about
    (same helper as tests/test_tab_rendering.py)."""
    for _ in range(20):
        at.run()
        seeded = False
        for exc in at.exception:
            m = re.search(r'st\.session_state has no key "([^"]+)"', str(exc.value))
            if m:
                at.session_state[m.group(1)] = ""
                seeded = True
        if not seeded:
            return at
    raise AssertionError("could not seed session state for the app run")


def _no_errors(at):
    assert list(at.exception) == [], [str(e.value)[:300] for e in at.exception]


class _GitHubStub:
    """What app.py's ``from core.catalog_storage import …`` binds to inside
    the AppTest script: a fresh-file fetch that returns ``csv_bytes`` and a
    save that records the CSV it was handed."""

    def __init__(self, monkeypatch, csv_bytes: bytes):
        self.csv_bytes = csv_bytes
        self.pushed: list[dict] = []
        monkeypatch.setattr(catalog_storage, "fetch_catalog_csv_from_github",
                            lambda file_path, token: (self.csv_bytes, "sha-fresh"))
        monkeypatch.setattr(catalog_storage, "latest_catalog_sha", lambda file_path, token: "sha-fresh")
        monkeypatch.setattr(catalog_storage, "save_catalog_to_github", self._save)

    def _save(self, csv_text, file_path, token, message, sha=None):
        self.pushed.append({"csv": csv_text, "path": file_path, "message": message, "sha": sha})
        return {"commit": {"sha": "abc1234def", "html_url": "https://example.test/c"},
                "content": {"sha": "sha-after"}}


@pytest.fixture
def data_setup(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    monkeypatch.setattr(usage_tracker, "USAGE_LOG_PATH", tmp_path / "usage_log.jsonl")

    def open_view(view: str):
        at = AppTest.from_file(str(APP_PATH), default_timeout=300)
        at.secrets["github_token"] = "tok"
        at.session_state[app.ACTIVE_TAB_KEY] = app.TAB_DATA_SETUP
        _run(at)
        picker = next(r for r in at.main.radio if r.label == "View")
        picker.set_value(view)
        _run(at)
        _no_errors(at)
        return at

    return open_view


def _submit(at, label_part: str):
    buttons = [b for b in at.main.button if label_part in b.label]
    assert len(buttons) == 1, [b.label for b in at.main.button]
    buttons[0].click()
    _run(at)
    _no_errors(at)


def _station_frames(at) -> list:
    return [d.value for d in at.main.dataframe if "Station" in list(d.value.columns)]


class TestAddNewMachineSkuForm:
    def test_the_committed_row_is_fully_routed_and_previewed(self, data_setup, monkeypatch):
        gh = _GitHubStub(monkeypatch, MACHINE_CSV.read_bytes())
        at = data_setup("🏭 Machine catalog")
        assert any("seeded from the most similar SKU" in c.value for c in at.main.caption)

        at.text_input(key="new_sku_machine").set_value("PDS999-001")
        at.text_input(key="new_desc_machine").set_value("PDS 999 COMPRESSOR")
        _submit(at, "Add machine SKU")

        assert any("PDS999-001" in s.value for s in at.main.success), [s.value for s in at.main.success]
        assert len(gh.pushed) == 1 and gh.pushed[0]["path"] == "data/machine_clean.csv"
        pushed = _pushed_frame(gh.pushed[0]["csv"]).set_index("SKU")
        row = pushed.loc["PDS999-001"]
        _assert_fully_routed(row, "PDS999-001")
        assert {c: row[c] for c in COLS} == _expected("PDS999-001", "PDS 999 COMPRESSOR")
        assert row["Final Station HND"] == "Undercarriage"
        assert row["Undercarriage Station HND"] == "ComAcc"
        assert row["Wire Station CYP"] == "ComAcc"
        assert row["Description"] == "PDS 999 COMPRESSOR"

        # The planner sees what the SKU got: stations × plants.
        frames = _station_frames(at)
        assert len(frames) == 1
        shown = frames[0].set_index("Station")
        assert list(shown.columns) == ["HND", "SPB", "CYP"]
        assert shown.at["Final", "HND"] == "Undercarriage"
        assert any("54 PDS Standard row(s)" in c.value for c in at.main.caption)


class TestAddXxxPlaceholderForm:
    @staticmethod
    def _accessory_csv_with_routing() -> bytes:
        # The accessory catalog has no routing columns, so the real form seeds
        # nothing there; give it a routing-bearing file to prove the wiring —
        # the placeholder then inherits the family's routing like any row.
        acc = pd.read_csv(ACC_CSV, dtype=str, keep_default_na=False)
        for c in COLS:
            acc[c] = ""
        is_boss25 = acc["SKU"].str.upper().str.startswith("BOSS25")
        assert is_boss25.any()
        acc.loc[is_boss25, "AccKIT Station HND"] = "GenAcc"
        acc.loc[is_boss25, "Wire Station CYP"] = "ComAcc"
        return acc.to_csv(index=False).encode("utf-8")

    def test_the_committed_row_is_fully_routed_and_previewed(self, data_setup, monkeypatch):
        gh = _GitHubStub(monkeypatch, self._accessory_csv_with_routing())
        at = data_setup("🔌 Accessory catalog")
        at.selectbox(key="placeholder_family").set_value("BOSS25")
        _run(at)
        _submit(at, "AXXX")

        assert any("BOSS25 AXXX" in s.value for s in at.main.success), [s.value for s in at.main.success]
        assert len(gh.pushed) == 1 and gh.pushed[0]["path"] == "data/acc_clean.csv"
        pushed = _pushed_frame(gh.pushed[0]["csv"]).set_index("SKU")
        row = pushed.loc["BOSS25 AXXX"]
        _assert_fully_routed(row, "BOSS25 AXXX")
        assert row["AccKIT Station HND"] == "GenAcc"       # the BOSS25 rows' routing
        assert row["Wire Station CYP"] == "ComAcc"
        assert row["Final Station SPB"] == "Final"          # nothing set anywhere → default
        assert float(row["BattSubRaw"]) == 320              # the form's own defaults still land

        frames = _station_frames(at)
        assert len(frames) == 1
        assert frames[0].set_index("Station").at["AccKIT", "HND"] == "GenAcc"

    def test_the_real_accessory_file_gets_no_routing_columns(self, data_setup, monkeypatch):
        gh = _GitHubStub(monkeypatch, ACC_CSV.read_bytes())
        at = data_setup("🔌 Accessory catalog")
        at.selectbox(key="placeholder_family").set_value("SDG")
        _run(at)
        _submit(at, "AXXX")
        assert len(gh.pushed) == 1
        pushed = _pushed_frame(gh.pushed[0]["csv"])
        assert "SDG AXXX" in set(pushed["SKU"])
        assert not routing_columns_in(pushed.columns)
        assert _station_frames(at) == []


# ---------------------------------------------------------------------------
# Both forms build their row through the seeder (source-level guard)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fn", ["_render_add_new_sku", "_render_add_xxx_placeholder"])
def test_forms_seed_then_show(fn):
    src = inspect.getsource(getattr(app, fn))
    assert "seed = seed_facility_routing(fresh_df, new_sku, new_desc)" in src
    assert "seed.fill(new_row)" in src
    assert "_render_seeded_routing(seed)" in src
