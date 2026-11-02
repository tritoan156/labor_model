"""PRODUCTION MONTH as an Excel date serial.

``Henderson_Combined`` stores its month column as plain numbers — Excel's
days-since-1899-12-30 serial with the date format lost — so the loader saw
``46296`` (2026-10-01) instead of a date. Nothing parsed it, every such row
counted as undated, and the undated-unit rescue folded the whole sheet into
the plan month. ``_coerce_production_month`` now reads serials in a plausible
range as ``Mon-YY`` and leaves any other number alone.
"""
import io

import pandas as pd
import pytest

from core import excel_io
from core.data_loader import _coerce_production_month, load_schedule

SCHEDULE_COLS = [
    "LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY", "PRODUCTION MONTH",
]


def _coerce(*values) -> list:
    return _coerce_production_month(pd.Series(list(values), dtype=object)).tolist()


class TestExcelSerialMonths:
    @pytest.mark.parametrize("value, month", [
        ("46296", "Oct-26"),       # 2026-10-01, as a CSV cell
        ("46296.0", "Oct-26"),     # the same, through a float column
        (46296, "Oct-26"),         # a raw int cell
        (46296.0, "Oct-26"),
        ("46321", "Oct-26"),       # 2026-10-26 — any day of the month
        ("46327", "Nov-26"),
        ("46357", "Dec-26"),
        ("46266", "Sep-26"),
        ("46296.75", "Oct-26"),    # a datetime serial's time part is ignored
        ("32874", "Jan-90"),       # range edges
        ("73050", "Dec-99"),
    ])
    def test_serial_becomes_mon_yy(self, value, month):
        assert _coerce(value) == [month]

    @pytest.mark.parametrize("value", [
        "32873",     # 1989 — below the range
        "73051",     # 2100 — above it
        "5",         # a quantity
        "123456",    # six digits — an order number, not a date
        "46296.5.1",
        "4629a",
    ])
    def test_other_numbers_are_left_alone(self, value):
        assert _coerce(value) == [value]

    def test_existing_formats_still_parse(self):
        assert _coerce("Oct-26", "26-Sep", "2026-10-01", "10/8/2026", "", "NEED SN") == [
            "Oct-26", "26-Sep", "Oct-26", "Oct-26", "", "NEED SN",
        ]


class TestSerialMonthsThroughTheLoader:
    def _csv(self, months) -> io.BytesIO:
        rows = [["HENDERSON", "BOSS25-010", "", 1, m] for m in months]
        df = pd.DataFrame(rows, columns=SCHEDULE_COLS)
        return io.BytesIO(df.to_csv(index=False).encode("utf-8"))

    def test_serial_rows_keep_their_own_month(self):
        df = load_schedule(self._csv(["46296", "46327", "46327.0", "Dec-26"]),
                           location="HENDERSON")
        assert df["PRODUCTION MONTH"].tolist() == ["Oct-26", "Nov-26", "Nov-26", "Dec-26"]
        # Dated rows are not "undated" — nothing is folded into the plan month.
        assert df.attrs["unscheduled_recovered"]["count"] == 0

    def test_workbook_with_numeric_month_cells_round_trips(self):
        # The month cells are plain numbers in the workbook (no date format),
        # exactly like Henderson_Combined — not datetimes excel_io would render.
        book = io.BytesIO()
        rows = [SCHEDULE_COLS, ["HENDERSON", "BOSS25-010", None, 1, 46296],
                ["HENDERSON", "BOSS70-002", None, 2, 46327]]
        with pd.ExcelWriter(book, engine="openpyxl") as writer:
            pd.DataFrame(rows).to_excel(writer, sheet_name="Plant_Combined",
                                        index=False, header=False)
        csv_bytes, _ = excel_io.excel_to_schedule_csv(book.getvalue())
        df = load_schedule(io.BytesIO(csv_bytes), location="HENDERSON")
        assert df["PRODUCTION MONTH"].tolist() == ["Oct-26", "Nov-26"]
        assert df["BUILD QTY"].tolist() == [1, 2]
