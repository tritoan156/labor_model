"""End-to-end check of the Excel upload path through ``app._load_schedule_df``.

The unit tests in test_excel_io.py prove the conversion; these prove app.py
wires it in — the converted CSV becomes the persisted active schedule, the
sheet name reaches the confirmation pill, and a workbook with no usable sheet
keeps raising on later reruns instead of silently reviving the old schedule.

Importing app.py executes the Streamlit script in bare mode (ScriptRunContext
warnings, but it loads — same approach as test_app_helpers). Skips if app
can't import.
"""
from __future__ import annotations

import datetime as dt
import io
from types import SimpleNamespace

import pandas as pd
import pytest

app = pytest.importorskip("app")
import streamlit as st  # noqa: E402

from core.data_loader import ScheduleColumnError  # noqa: E402

HEADER = ["Location", "FG SKU", "Accessory SKU", "Qty", "Prod Month", "Build Date"]
ROW_A = ["HENDERSON", "BOSS25-010", "BOSS25-A016", 5.0, dt.datetime(2026, 5, 1), dt.datetime(2026, 5, 8)]
ROW_B = ["HENDERSON", "BOSS70-002", None, 2, "May-26", None]


def _workbook(sheets: dict[str, list[list]]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False, header=False)
    return buf.getvalue()


def _upload(name: str, data: bytes, file_id: str):
    """Stand-in for Streamlit's UploadedFile: the three attributes app.py reads."""
    return SimpleNamespace(name=name, file_id=file_id, size=len(data), getvalue=lambda: data)


@pytest.fixture(autouse=True)
def _fresh_session_state():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    yield
    for key in list(st.session_state.keys()):
        del st.session_state[key]


class TestExcelUploadThroughApp:
    def test_workbook_upload_becomes_the_active_csv(self):
        book = _workbook({
            "Cover": [["Henderson plan"]],
            "Plan": [["Production plan"], [None], HEADER, ROW_A, ROW_B],
        })
        df = app._load_schedule_df(
            _upload("plan.xlsx", book, "file-1"), location="Henderson",
            target_month_hint=(2026, 5),
        )
        assert df["FG_BASE"].tolist() == ["BOSS25-010", "BOSS70-002"]
        assert df["BUILD QTY"].tolist() == [5, 2]
        # What persists for later reruns / 💾 Save is the converted CSV, not xlsx bytes.
        assert st.session_state["_active_schedule_csv"].startswith(b"Location,FG SKU")
        assert st.session_state["_active_schedule_source"] == "upload"
        assert st.session_state["_active_schedule_sheet"] == "Plan"
        summary = st.session_state["_last_upload_summary"]
        assert summary["source"] == "upload" and summary["sheet"] == "Plan"
        assert summary["rows"] == 2

    def test_same_workbook_on_rerun_falls_through_to_active_csv(self):
        book = _workbook({"Plan": [HEADER, ROW_A]})
        up = _upload("plan.xlsx", book, "file-2")
        app._load_schedule_df(up, location="Henderson", target_month_hint=(2026, 5))
        first = st.session_state["_active_schedule_csv"]
        # The uploader keeps returning the same object across reruns — the
        # second call must not reconvert / clobber, just reuse the active CSV.
        df = app._load_schedule_df(up, location="Henderson", target_month_hint=(2026, 5))
        assert st.session_state["_active_schedule_csv"] is first
        assert st.session_state["_active_schedule_sheet"] == "Plan"
        assert len(df) == 1

    def test_bad_workbook_keeps_raising_on_rerun(self):
        bad = _workbook({"Totals": [["Qty", "Month"], [1, "May-26"]]})
        up = _upload("bad.xlsx", bad, "file-3")
        with pytest.raises(ScheduleColumnError) as first:
            app._load_schedule_df(up, location="Henderson")
        assert "bad.xlsx" in str(first.value)
        assert "_active_schedule_csv" not in st.session_state
        # Same file still sitting in the uploader on the next rerun (any widget
        # nudge): the banner must persist, not fall back to the bundled sample.
        with pytest.raises(ScheduleColumnError) as again:
            app._load_schedule_df(up, location="Henderson")
        assert str(again.value) == str(first.value)

    def test_good_upload_clears_an_earlier_failure(self):
        bad = _workbook({"Totals": [["Qty"], [1]]})
        with pytest.raises(ScheduleColumnError):
            app._load_schedule_df(_upload("bad.xlsx", bad, "file-4"), location="Henderson")
        good = _workbook({"Plan": [HEADER, ROW_A]})
        df = app._load_schedule_df(
            _upload("plan.xlsx", good, "file-5"), location="Henderson",
            target_month_hint=(2026, 5),
        )
        assert len(df) == 1
        assert "_active_schedule_error" not in st.session_state

    def test_csv_upload_is_untouched(self):
        csv = (b"LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH\n"
               b"HENDERSON,BOSS25-010,,3,May-26\n")
        df = app._load_schedule_df(
            _upload("plan.csv", csv, "file-6"), location="Henderson",
            target_month_hint=(2026, 5),
        )
        assert st.session_state["_active_schedule_csv"] is csv
        assert st.session_state["_active_schedule_sheet"] is None
        assert st.session_state["_last_upload_summary"].get("sheet") is None
        assert df["BUILD QTY"].tolist() == [3]


class TestExportHelpers:
    def test_session_settings_rows_keep_scalars_only(self):
        inputs = {
            "location": "Henderson", "days": 22, "efficiency": 0.5, "absorb": True,
            "plan_month": (2026, 10), "plan_month_label": "October 2026",
            "excluded_stations": ["QC", "Ship"],
            "crew_config": pd.DataFrame({"HC": [1]}), "uploaded": None,
        }
        rows = {r["Setting"]: r["Value"] for r in app._session_settings_rows(inputs)}
        assert rows["location"] == "Henderson" and rows["days"] == 22
        assert rows["excluded_stations"] == "QC; Ship"
        assert "plan_month" not in rows and "crew_config" not in rows and "uploaded" not in rows

    def test_database_export_signature_tracks_every_data_file(self):
        files = {f for _s, f, _d in app.EXCEL_DATABASE_TABLES}
        assert set(app._DATABASE_EXPORT_FILES) == files
        sig_a = app._database_export_signature(False)
        sig_b = app._database_export_signature(True)
        assert sig_a != sig_b and sig_a[0] == sig_b[0]
