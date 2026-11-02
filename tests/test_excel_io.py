"""Tests for core.excel_io — Excel schedule uploads and the database export.

Inbound: an ``.xlsx`` upload has to land in the loader as the same CSV bytes
a planner's "Save As CSV" would have produced — header found wherever it is,
dates and quantities rendered the way the loader already parses them.

Outbound: every data file flattens to a plain table, and the workbook writer
copes with whatever those tables carry (duplicate labels, lists, tz-aware
timestamps, illegal sheet names).
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import pytest

from core import excel_io
from core.data_loader import ScheduleColumnError, load_schedule

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HEADER = ["Location", "FG SKU", "Accessory SKU", "Qty", "Prod Month", "Build Date"]
ROW_A = ["HENDERSON", "BOSS25-010", "BOSS25-A016", 5.0, dt.datetime(2026, 5, 1), dt.datetime(2026, 5, 8)]
ROW_B = ["HENDERSON", "BOSS70-002", None, 2, "May-26", None]


def _workbook(sheets: dict[str, list[list]]) -> bytes:
    """Build an .xlsx in memory from {sheet name: raw rows} (no header handling —
    rows are written exactly as given so tests control where the header sits)."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False, header=False)
    return buf.getvalue()


def _csv_frame(csv_bytes: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------
class TestIsExcelFilename:
    @pytest.mark.parametrize("name", ["plan.xlsx", "PLAN.XLSX", "macro.xlsm", " x.xlsx "])
    def test_workbook_extensions(self, name):
        assert excel_io.is_excel_filename(name)

    @pytest.mark.parametrize("name", ["plan.csv", "plan.xls", "plan", "", None])
    def test_everything_else(self, name):
        # .xls needs xlrd, which isn't a dependency — treated as not-Excel so
        # the uploader's type filter (not this helper) is what rejects it.
        assert not excel_io.is_excel_filename(name)


class TestExcelToScheduleCsv:
    def test_header_on_first_row(self):
        csv_bytes, sheet = excel_io.excel_to_schedule_csv(_workbook({"Sheet1": [HEADER, ROW_A]}))
        assert sheet == "Sheet1"
        df = _csv_frame(csv_bytes)
        assert list(df.columns) == HEADER
        assert len(df) == 1

    def test_skips_cover_sheet_and_title_rows(self):
        book = _workbook({
            "Cover": [["Henderson plan — read me"], ["nothing here"]],
            "Plan": [["Production plan", None], [None, None], HEADER, ROW_A, ROW_B],
        })
        csv_bytes, sheet = excel_io.excel_to_schedule_csv(book, "plan.xlsx")
        assert sheet == "Plan"
        df = _csv_frame(csv_bytes)
        assert list(df.columns) == HEADER
        assert df["FG SKU"].tolist() == ["BOSS25-010", "BOSS70-002"]

    def test_cell_types_render_like_a_csv_export(self):
        csv_bytes, _ = excel_io.excel_to_schedule_csv(_workbook({"S": [HEADER, ROW_A, ROW_B]}))
        df = _csv_frame(csv_bytes)
        # Whole-number float quantity → "5", not "5.0"; integer stays "2".
        assert df["Qty"].tolist() == ["5", "2"]
        # Date cells → ISO date, no midnight time part; text months untouched.
        assert df["Prod Month"].tolist() == ["2026-05-01", "May-26"]
        assert df["Build Date"].tolist() == ["2026-05-08", ""]
        # Blank accessory cell → empty string, not "nan"/"None".
        assert df["Accessory SKU"].tolist() == ["BOSS25-A016", ""]

    def test_blank_rows_dropped_and_blank_headers_named(self):
        header = HEADER + [None]
        rows = [header, ROW_A + ["note"], [None] * 7, ["", "", "", None, "", "", ""]]
        csv_bytes, _ = excel_io.excel_to_schedule_csv(_workbook({"S": rows}))
        df = _csv_frame(csv_bytes)
        assert len(df) == 1
        assert df.columns[-1] == "Unnamed: 6"

    def test_no_usable_sheet_names_the_sheets_it_tried(self):
        book = _workbook({"Cover": [["hello"]], "Totals": [["Qty", "Month"], [1, "May-26"]]})
        with pytest.raises(ScheduleColumnError) as exc:
            excel_io.excel_to_schedule_csv(book, "plan.xlsx")
        msg = str(exc.value)
        assert "plan.xlsx" in msg and "`Cover`" in msg and "`Totals`" in msg
        assert "Production Month" in msg

    def test_header_below_scan_window_is_not_found(self):
        rows = [[None] * 6] * (excel_io.HEADER_SCAN_ROWS + 1) + [HEADER, ROW_A]
        with pytest.raises(ScheduleColumnError):
            excel_io.excel_to_schedule_csv(_workbook({"S": rows}))

    def test_garbage_bytes_raise_planner_friendly_error(self):
        with pytest.raises(ScheduleColumnError) as exc:
            excel_io.excel_to_schedule_csv(b"definitely not a zip", "bad.xlsx")
        assert "bad.xlsx" in str(exc.value)

    def test_converted_csv_loads_through_the_real_loader(self):
        csv_bytes, _ = excel_io.excel_to_schedule_csv(_workbook({"S": [HEADER, ROW_A, ROW_B]}))
        df = load_schedule(io.BytesIO(csv_bytes), location="HENDERSON")
        assert df["FG_BASE"].tolist() == ["BOSS25-010", "BOSS70-002"]
        assert df["BUILD QTY"].tolist() == [5, 2]
        assert df["PRODUCTION MONTH"].tolist() == ["May-26", "May-26"]
        assert df["PRODUCTION DAY"].iloc[0] == pd.Timestamp("2026-05-08")
        assert pd.isna(df["PRODUCTION DAY"].iloc[1])


def _plan_rows(n: int, sku: str = "BOSS25-010", qty=1) -> list[list]:
    """A header plus ``n`` one-unit data rows — a schedule-shaped sheet."""
    return [HEADER] + [["HENDERSON", sku, None, qty, "Oct-26", None] for _ in range(n)]


class TestSheetChoice:
    """Henderson's workbook carries the schedule header on six tabs: one per
    family, a CarryOver tab, an Archive, and ``Henderson_Combined`` (the sum
    of the family tabs). Reading the first match took the Generators tab — a
    fifth of the month — with no warning."""

    def test_combined_sheet_beats_family_sheets_that_come_first(self):
        book = _workbook({
            "Dashboard": [["totals"]],
            "Plant_Generators_2026": _plan_rows(2, "SDG65-006UR"),
            "Plant_EBOSS_2026": _plan_rows(5),
            "Plant_Archive": _plan_rows(9),
            "Plant_Combined": _plan_rows(7),
        })
        wb = excel_io.read_schedule_workbook(book, "plan.xlsx")
        assert wb.sheet.name == "Plant_Combined"
        assert len(_csv_frame(wb.csv_bytes)) == 7
        assert [s.name for s in wb.skipped] == [
            "Plant_Generators_2026", "Plant_EBOSS_2026", "Plant_Archive",
        ]

    def test_whole_plan_name_wins_over_a_bigger_sheet(self):
        book = _workbook({"Big": _plan_rows(9), "All Units": _plan_rows(3)})
        assert excel_io.read_schedule_workbook(book).sheet.name == "All Units"

    @pytest.mark.parametrize("name", ["Install", "Ball park", "Uncombined"])
    def test_whole_plan_words_match_whole_words_only(self, name):
        book = _workbook({name: _plan_rows(1), "Bigger": _plan_rows(4)})
        assert excel_io.read_schedule_workbook(book).sheet.name == "Bigger"

    def test_without_a_named_sheet_the_most_units_wins(self):
        book = _workbook({
            "Gens": _plan_rows(2),
            "EBOSS": _plan_rows(1, qty=6),
            "Comps": _plan_rows(4),
        })
        wb = excel_io.read_schedule_workbook(book)
        assert wb.sheet.name == "EBOSS"
        assert (wb.sheet.rows, wb.sheet.units) == (1, 6)

    def test_units_count_only_rows_the_loader_would_keep(self):
        rows = _plan_rows(2) + [
            ["HENDERSON", "BOSS25-010", None, 0, "Oct-26", None],
            ["HENDERSON", "BOSS25-010", None, None, "Oct-26", None],
            ["HENDERSON", "BOSS25-010", None, "TBD", "Oct-26", None],
        ]
        wb = excel_io.read_schedule_workbook(_workbook({"S": rows, "T": _plan_rows(3)}))
        assert wb.sheet.name == "T"
        assert (wb.skipped[0].rows, wb.skipped[0].units) == (2, 2)

    def test_archive_is_read_only_when_nothing_else_matches(self):
        book = _workbook({"Live": _plan_rows(1), "Archive 2025": _plan_rows(9)})
        assert excel_io.read_schedule_workbook(book).sheet.name == "Live"
        only = _workbook({"Cover": [["x"]], "Old plan": _plan_rows(2)})
        wb = excel_io.read_schedule_workbook(only)
        assert wb.sheet.name == "Old plan" and wb.skipped == ()

    def test_ties_go_to_workbook_order(self):
        book = _workbook({"First": _plan_rows(2), "Second": _plan_rows(2)})
        assert excel_io.read_schedule_workbook(book).sheet.name == "First"

    def test_note_names_the_read_sheet_and_every_skipped_one(self):
        book = _workbook({
            "Gens": _plan_rows(2), "Old_Archive": _plan_rows(3), "Combined": _plan_rows(5),
        })
        note = excel_io.read_schedule_workbook(book).note
        assert "`Combined` (5 units in 5 rows)" in note
        assert "whole plan" in note
        assert "`Gens` (2 units)" in note
        assert "`Old_Archive` (3 units, archive)" in note

    def test_single_matching_sheet_has_no_note(self):
        book = _workbook({"Cover": [["x"]], "Plan": _plan_rows(2)})
        wb = excel_io.read_schedule_workbook(book)
        assert wb.skipped == () and wb.note == ""

    def test_excel_to_schedule_csv_returns_the_chosen_sheet(self):
        book = _workbook({"Gens": _plan_rows(1), "Combined": _plan_rows(3)})
        csv_bytes, sheet = excel_io.excel_to_schedule_csv(book)
        assert sheet == "Combined"
        assert len(_csv_frame(csv_bytes)) == 3


class TestUploadToScheduleCsv:
    def test_csv_passes_through_byte_for_byte(self):
        raw = b"\xef\xbb\xbfLOCATION,FG SKU ID\r\nHENDERSON,BOSS25-010\r\n"
        out, sheet = excel_io.upload_to_schedule_csv("plan.csv", raw)
        assert out is raw and sheet is None

    def test_workbook_is_converted(self):
        out, sheet = excel_io.upload_to_schedule_csv("plan.xlsx", _workbook({"Plan": [HEADER, ROW_A]}))
        assert sheet == "Plan"
        assert out.startswith(b"Location,FG SKU")


# ---------------------------------------------------------------------------
# Outbound — flatteners
# ---------------------------------------------------------------------------
class TestFlatteners:
    def test_crew_table(self):
        cfg = {"Henderson": {"QC": {"HC": 4, "Conc": 2, "Crew": 1}},
               "Cypress": {"QC": {"HC": 1, "Conc": 1, "Crew": 1}, "PDI": {"HC": 2, "Conc": 1, "Crew": 1}}}
        df = excel_io.crew_table(cfg)
        assert list(df.columns) == ["Facility", "Station", "HC", "Conc", "Crew"]
        assert len(df) == 3
        assert df.loc[df["Facility"] == "Henderson", "HC"].tolist() == [4]

    def test_crew_table_empty(self):
        assert excel_io.crew_table({}).empty
        assert list(excel_io.crew_table(None).columns) == ["Facility", "Station", "HC", "Conc", "Crew"]

    def test_settings_table_joins_lists(self):
        df = excel_io.settings_table({"Henderson": {"absenteeism": 0.05, "excluded_stations": ["QC", "Ship"]}})
        by_key = dict(zip(df["Setting"], df["Value"]))
        assert by_key["absenteeism"] == 0.05
        assert by_key["excluded_stations"] == "QC; Ship"

    def test_scenarios_table_one_row_per_entry(self):
        s = {"Henderson": {"June": {"saved_at": "t1", "entries": [
                {"FG SKU": "A", "Accessory SKU": "", "Qty": 5},
                {"FG SKU": "B", "Accessory SKU": "B-A", "Qty": 1}]},
              "Empty": {"saved_at": "t2", "entries": []}},
             "Cypress": {}}
        df = excel_io.scenarios_table(s)
        assert list(df.columns[:3]) == ["Facility", "Scenario", "Saved at"]
        assert len(df) == 3  # two entries + one placeholder row for the empty scenario
        assert df.loc[df["Scenario"] == "June", "FG SKU"].tolist() == ["A", "B"]

    def test_exec_scenarios_table_long_format(self):
        e = {"Spartanburg": {"Jun": {"saved_at": "t",
                                     "assumptions": {"units": 100, "nested": {"a": 1}},
                                     "metrics": {"verdict": "OK", "coverage_pct": 0.9}}}}
        df = excel_io.exec_scenarios_table(e)
        assert len(df) == 4
        assert set(df["Section"]) == {"Assumption", "Metric"}
        nested = df.loc[df["Name"] == "nested", "Value"].iloc[0]
        assert nested == '{"a": 1}'

    def test_saved_schedules_index_and_rows_union_columns(self):
        u = {"Henderson": {"Jun": {"saved_at": "t1", "rows": 1,
                                   "csv_content": "LOCATION,FG SKU ID,BUILD QTY\nHENDERSON,A,1\n"},
                           "Jul": {"saved_at": "t2", "rows": 2,
                                   "csv_content": "﻿LOCATION,FG SKU ID,CUSTOMER NAME\nHENDERSON,B,X\nHENDERSON,C,Y\n"},
                           "Blank": {"saved_at": "t3", "rows": 0, "csv_content": ""}}}
        idx = excel_io.saved_schedules_index(u)
        assert idx["Schedule"].tolist() == ["Jun", "Jul", "Blank"]
        rows = excel_io.saved_schedule_rows(u)
        assert list(rows.columns[:2]) == ["Facility", "Schedule"]
        assert len(rows) == 3
        assert {"BUILD QTY", "CUSTOMER NAME"} <= set(rows.columns)
        # BOM on the second blob stripped from its header.
        assert "﻿LOCATION" not in rows.columns
        assert rows.loc[rows["Schedule"] == "Jul", "CUSTOMER NAME"].tolist() == ["X", "Y"]

    def test_process_flow_table(self):
        df = excel_io.process_flow_table([{"From": "Warehouse", "To": "Wire", "Class": "All"}])
        assert list(df.columns) == ["From", "To", "Class"]
        assert excel_io.process_flow_table([]).empty


# ---------------------------------------------------------------------------
# Outbound — workbook writer
# ---------------------------------------------------------------------------
class TestSafeSheetName:
    def test_strips_illegal_chars_and_truncates(self):
        assert excel_io.safe_sheet_name("A/B:C*D?[E]\\F") == "ABCDEF"
        assert len(excel_io.safe_sheet_name("x" * 50)) == 31

    def test_dedupes_against_taken(self):
        taken = {"Sheet", "Sheet (2)"}
        assert excel_io.safe_sheet_name("Sheet", taken) == "Sheet (3)"

    def test_empty_falls_back(self):
        assert excel_io.safe_sheet_name("") == "Sheet"


class TestBuildWorkbook:
    def test_readme_first_and_names_sanitized(self):
        wb = excel_io.build_workbook(
            {"Bad/Name": pd.DataFrame({"a": [1]}), "Bad:Name": pd.DataFrame({"b": [2]})},
            readme=pd.DataFrame({"Sheet": ["x"]}),
        )
        book = pd.read_excel(io.BytesIO(wb), sheet_name=None)
        assert list(book) == ["README", "BadName", "BadName (2)"]

    def test_awkward_cells_survive(self):
        df = pd.DataFrame({
            "when": pd.to_datetime(["2026-05-08"]).tz_localize("America/Los_Angeles"),
            "tags": [["a", "b"]],
            "n": [1.5],
        })
        df.index.name = "station"
        wb = excel_io.build_workbook({"T": df})
        back = pd.read_excel(io.BytesIO(wb), sheet_name="T")
        assert list(back.columns) == ["station", "when", "tags", "n"]
        assert back["tags"].iloc[0] == '["a", "b"]'
        assert pd.Timestamp(back["when"].iloc[0]) == pd.Timestamp("2026-05-08")

    def test_duplicate_column_labels_disambiguated(self):
        df = pd.DataFrame([[1, 2]], columns=["Qty", "Qty"])
        wb = excel_io.build_workbook({"T": df})
        back = pd.read_excel(io.BytesIO(wb), sheet_name="T")
        assert list(back.columns) == ["Qty", "Qty.1"]

    def test_empty_frame_still_gets_a_sheet(self):
        wb = excel_io.build_workbook({"Empty": pd.DataFrame(columns=["a", "b"])})
        back = pd.read_excel(io.BytesIO(wb), sheet_name="Empty")
        assert list(back.columns) == ["a", "b"] and back.empty


class TestDatabaseWorkbook:
    def test_every_table_lands_as_a_sheet(self):
        wb = excel_io.build_database_workbook(include_saved_schedule_rows=False)
        book = pd.read_excel(io.BytesIO(wb), sheet_name=None)
        expected = ["README"] + [s for s, _f, _d in excel_io.DATABASE_TABLES
                                 if s != excel_io.SAVED_SCHEDULE_ROWS_SHEET]
        assert list(book) == expected
        # The catalog sheet is the CSV verbatim: same row count, same columns.
        csv = pd.read_csv(DATA_DIR / "machine_clean.csv", dtype=str, keep_default_na=False)
        assert book["Machine catalog"].shape == csv.shape
        assert list(book["Machine catalog"].columns) == list(csv.columns)
        # README names every sheet and links the CSV ones.
        readme = book["README"]
        assert set(readme["Sheet"]) == set(expected) - {"README"}
        link_col = [c for c in readme.columns if c.startswith("Live link")][0]
        machine_link = readme.loc[readme["Sheet"] == "Machine catalog", link_col].iloc[0]
        assert machine_link.endswith("/data/machine_clean.csv")

    def test_saved_schedule_rows_opt_in(self):
        tables = excel_io.database_tables(include_saved_schedule_rows=True)
        assert excel_io.SAVED_SCHEDULE_ROWS_SHEET in tables
        assert excel_io.SAVED_SCHEDULE_ROWS_SHEET not in excel_io.database_tables(
            include_saved_schedule_rows=False)

    def test_csv_tables_can_come_from_another_dir(self, tmp_path):
        (tmp_path / "machine_clean.csv").write_text("SKU,Description\nX,Y\n", encoding="utf-8")
        tables = excel_io.database_tables(tmp_path, include_saved_schedule_rows=False)
        assert tables["Machine catalog"]["SKU"].tolist() == ["X"]
        assert tables["Accessory catalog"].empty  # missing file → empty sheet, no crash


class TestLinkHelpers:
    def test_raw_github_url(self):
        url = excel_io.raw_github_url("machine_clean.csv", repo="o/r", branch="staging")
        assert url == "https://raw.githubusercontent.com/o/r/staging/data/machine_clean.csv"

    def test_power_query_snippet(self):
        m = excel_io.power_query_m_for_csv("https://example.test/x.csv")
        assert 'Web.Contents("https://example.test/x.csv")' in m
        assert "Table.PromoteHeaders" in m and m.startswith("let")
