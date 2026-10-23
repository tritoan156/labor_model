"""``load_schedule`` accounts for every row it drops.

The sidebar pill used to say "Loaded 277 rows" for a 288-row CSV and leave
the planner to guess where 11 rows went (they had no BUILD QTY). The loader
now records the file's data-row count and the rows dropped for having no
quantity — plus rows left out for being tagged with another plant — in
``.attrs`` next to the recovery summaries, so the pill can say "Loaded 277 of
288 rows" and explain the difference.
"""
import io

import pandas as pd
import pytest

from core.data_loader import load_schedule
from core.uploaded_schedule_storage import count_csv_data_rows

SCHEDULE_COLS = [
    "LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY", "PRODUCTION MONTH",
]
ROW_COUNT_ATTRS = ("source_rows", "no_qty_rows", "other_location_rows")


def _row(sku, qty, location="HENDERSON"):
    return {
        "LOCATION": location, "FG SKU ID": sku, "FG ACCRY SKU ID": "",
        "BUILD QTY": qty, "PRODUCTION MONTH": "Jul-26",
    }


def _csv(rows) -> io.BytesIO:
    df = pd.DataFrame(rows, columns=SCHEDULE_COLS)
    return io.BytesIO(df.to_csv(index=False).encode("latin-1"))


def _counts(df) -> dict:
    return {k: df.attrs.get(k) for k in ROW_COUNT_ATTRS}


class TestBundledSample:
    def test_sample_loads_every_row_and_drops_none_for_quantity(self):
        df = load_schedule()
        assert _counts(df) == {"source_rows": 193, "no_qty_rows": 0, "other_location_rows": 0}
        assert len(df) == df.attrs["source_rows"]

    def test_attrs_sit_next_to_the_recovery_summaries(self):
        df = load_schedule()
        for key in ROW_COUNT_ATTRS + (
            "placeholder_recovered", "unscheduled_recovered",
            "location_recovered", "foreign_location_recovered",
        ):
            assert key in df.attrs, key
        assert all(isinstance(df.attrs[k], int) for k in ROW_COUNT_ATTRS)


class TestNoQtyRows:
    def test_blank_zero_negative_and_junk_quantities_are_counted(self):
        df = load_schedule(_csv([
            _row("BOSS25-010", 2),
            _row("BOSS25-011", ""),      # blank
            _row("BOSS25-012", 0),       # zero
            _row("BOSS25-013", -2),      # negative
            _row("BOSS25-014", "abc"),   # section divider / junk
            _row("BOSS25-015", "inf"),   # non-finite
            _row("BOSS25-016", 3),
        ]), location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-010", "BOSS25-016"]
        assert _counts(df) == {"source_rows": 7, "no_qty_rows": 5, "other_location_rows": 0}
        # The reconciliation the pill prints: loaded + dropped = source.
        assert len(df) + df.attrs["no_qty_rows"] == df.attrs["source_rows"]

    def test_rows_with_a_quantity_but_no_sku_are_not_no_qty_rows(self):
        # Those are the placeholder-recovery path's business (reported in
        # units); counting them here would double-report the gap.
        df = load_schedule(_csv([
            _row("BOSS25-010", 1),
            _row("", 4),
            _row(None, ""),   # neither SKU nor qty → dropped for no qty
        ]), location="HENDERSON")
        assert len(df) == 1
        assert df.attrs["no_qty_rows"] == 1
        assert df.attrs["source_rows"] == 3

    def test_counts_cover_the_whole_file_not_just_this_plant(self):
        # A blank-qty row for another plant is still a row the file had.
        df = load_schedule(_csv([
            _row("BOSS25-010", 1),
            _row("BOSS25-011", "", location="CYPRESS"),
        ]), location="HENDERSON")
        assert _counts(df) == {"source_rows": 2, "no_qty_rows": 1, "other_location_rows": 0}


class TestOtherLocationRows:
    ROWS = [
        _row("BOSS25-010", 1),
        _row("BOSS25-011", 2, location="CYPRESS"),
        _row("BOSS25-012", 3, location="SPARTANBURG"),
        _row("BOSS25-013", 4, location=""),   # untagged → folded in, not "other"
    ]

    def test_strict_filtering_counts_the_rows_it_leaves_out(self):
        df = load_schedule(_csv(self.ROWS), location="HENDERSON")
        assert df["FG SKU ID"].tolist() == ["BOSS25-010", "BOSS25-013"]
        assert df.attrs["other_location_rows"] == 2
        assert len(df) + df.attrs["other_location_rows"] == df.attrs["source_rows"]

    def test_absorbing_them_leaves_nothing_to_report(self):
        df = load_schedule(_csv(self.ROWS), location="HENDERSON",
                           absorb_foreign_locations=True)
        assert len(df) == 4
        assert df.attrs["other_location_rows"] == 0
        assert df.attrs["foreign_location_recovered"]["rows"] == 2


class TestSourceRowsMatchTheStorageCount:
    def test_a_quoted_multi_line_cell_is_one_row_for_both(self):
        csv_text = (
            "LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,CUSTOMER NAME\n"
            'HENDERSON,BOSS25-010,,1,Jul-26,"ACME\nsecond line of the note"\n'
            "HENDERSON,BOSS25-011,,2,Jul-26,BETA\n"
            "\n"
        )
        df = load_schedule(io.BytesIO(csv_text.encode("latin-1")), location="HENDERSON")
        assert len(df) == 2
        assert df.attrs["source_rows"] == 2 == count_csv_data_rows(csv_text)
        # …where the old line-break count said 3.
        assert sum(1 for l in csv_text.splitlines() if l.strip()) - 1 == 3

    @pytest.mark.parametrize("location", ["Henderson", "Spartanburg", "Cypress"])
    def test_every_bundled_saved_schedule_agrees(self, location):
        from core.uploaded_schedule_storage import load_all_uploaded_schedules
        blocks = load_all_uploaded_schedules().get(location) or {}
        if not blocks:
            pytest.skip(f"no saved schedules for {location}")
        for name, block in blocks.items():
            csv_text = block["csv_content"]
            df = load_schedule(io.BytesIO(csv_text.encode("utf-8")), location=location.upper())
            assert df.attrs["source_rows"] == count_csv_data_rows(csv_text), name
            assert df.attrs["no_qty_rows"] >= 0
            assert len(df) + df.attrs["no_qty_rows"] <= df.attrs["source_rows"], name
