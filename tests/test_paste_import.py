"""Tests for the paste-from-Excel bulk SKU import (app._parse_pasted_skus).

The parser has to survive whatever a spreadsheet puts on the clipboard, so
these cover the shapes planners actually paste: tab-separated Excel cells,
CSV, a bare list of SKUs, headers or no headers, and rows with junk in them.
"""
import pandas as pd
import pytest

app = pytest.importorskip("app")

MACHINE = ["BOSS25-010", "BOSS70-002", "SDG25", "BOSS125-001", "PDS100"]
ACC = ["BOSS25-A016", "BOSS70-A001", "PDS100 AXXX"]


def parse(text, **kw):
    kw.setdefault("machine_skus", MACHINE)
    kw.setdefault("acc_skus", ACC)
    return app._parse_pasted_skus(text, **kw)


class TestDelimiters:
    def test_tab_separated_three_columns(self):
        df, rep = parse("BOSS25-010\tBOSS25-A016\t5\nBOSS70-002\t\t2")
        assert rep["delimiter"] == "tab"
        assert rep["rows"] == 2
        assert rep["units"] == 7
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]
        assert df.iloc[1].tolist() == ["BOSS70-002", "", 2]

    def test_comma_separated(self):
        df, rep = parse("BOSS25-010,BOSS25-A016,5")
        assert rep["delimiter"] == "comma"
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_semicolon_separated(self):
        df, rep = parse("BOSS25-010;BOSS25-A016;5")
        assert rep["delimiter"] == "semicolon"
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_space_aligned(self):
        df, rep = parse("BOSS25-010   BOSS25-A016   5")
        assert rep["delimiter"] == "spaces"
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_bare_list_one_sku_per_line(self):
        df, rep = parse("BOSS25-010\nBOSS70-002\nSDG25")
        assert rep["delimiter"] == "none"
        assert rep["rows"] == 3
        assert rep["units"] == 3                 # blank qty ⇒ 1 each
        assert rep["qty_defaulted"] == 3
        assert df["Accessory SKU"].tolist() == ["", "", ""]

    def test_single_line_of_skus_stands_up_vertically(self):
        # Someone copied a row instead of a column — 3+ SKUs, no quantity.
        df, rep = parse("BOSS25-010, BOSS70-002, SDG25")
        assert rep["rows"] == 3
        assert df["FG SKU"].tolist() == ["BOSS25-010", "BOSS70-002", "SDG25"]

    def test_single_row_with_qty_stays_horizontal(self):
        df, _ = parse("BOSS25-010\tBOSS25-A016\t5")
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]


class TestHeaders:
    def test_schedule_style_header(self):
        df, rep = parse(
            "FG SKU ID\tFG ACCRY SKU ID\tBUILD QTY\n"
            "BOSS25-010\tBOSS25-A016\t5"
        )
        assert rep["header"] is True
        assert rep["columns"] == {"FG SKU": 0, "Accessory SKU": 1, "Qty": 2}
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_header_out_of_order(self):
        df, rep = parse("Qty\tItem\nBOSS25-010\t3")
        # Header wins over position: col 0 is Qty even though it holds the SKU
        # cell in the data row... which means row 1 reads Qty=BOSS25-010.
        assert rep["header"] is True
        assert rep["columns"]["Qty"] == 0
        assert rep["columns"]["FG SKU"] == 1
        assert df.empty or df.iloc[0]["FG SKU"] == "3"

    def test_friendly_header_names(self):
        df, rep = parse("Model\tQuantity\nBOSS25-010\t4")
        assert rep["header"] is True
        assert df.iloc[0].tolist() == ["BOSS25-010", "", 4]

    def test_sku_row_is_not_mistaken_for_header(self):
        _, rep = parse("BOSS25-010\t5\nBOSS70-002\t2")
        assert rep["header"] is False
        assert rep["rows"] == 2

    def test_header_word_match_is_whole_word(self):
        # "SEAT" contains "EA" but must not be read as a quantity column.
        assert app._paste_header_role("SEAT") is None
        assert app._paste_header_role("EA") == "Qty"
        assert app._paste_header_role("Accessory SKU") == "Accessory SKU"
        assert app._paste_header_role("SKU") == "FG SKU"


class TestColumnDetection:
    def test_two_columns_sku_plus_qty(self):
        df, rep = parse("BOSS25-010\t5\nBOSS70-002\t2")
        assert rep["columns"]["Qty"] == 1
        assert rep["columns"]["Accessory SKU"] is None
        assert df["Qty"].tolist() == [5, 2]

    def test_two_columns_fg_plus_accessory(self):
        df, rep = parse("BOSS25-010\tBOSS25-A016\nBOSS70-002\tBOSS70-A001")
        assert rep["columns"]["Accessory SKU"] == 1
        assert rep["columns"]["Qty"] is None
        assert df["Accessory SKU"].tolist() == ["BOSS25-A016", "BOSS70-A001"]

    def test_accessory_column_first_is_swapped_back(self):
        df, _ = parse("BOSS25-A016\tBOSS25-010\t5")
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_extra_columns_are_ignored(self):
        # A real export: LOCATION, FG, ACC, QTY, MONTH, CUSTOMER — no header.
        df, rep = parse(
            "HENDERSON\tBOSS25-010\tBOSS25-A016\t5\tMay-26\tACME CORP\n"
            "HENDERSON\tBOSS70-002\tBOSS70-A001\t2\tMay-26\tACME CORP"
        )
        assert rep["columns"] == {"FG SKU": 1, "Accessory SKU": 2, "Qty": 3}
        assert df["Qty"].tolist() == [5, 2]

    def test_date_column_is_never_read_as_qty(self):
        _, rep = parse("BOSS25-010\t5/4/2026\nBOSS70-002\t5/5/2026")
        assert rep["columns"]["Qty"] is None
        assert rep["units"] == 2                 # both defaulted to 1

    def test_unknown_skus_still_place_columns_by_shape(self):
        df, rep = parse("ZZZ99-001\t4\nZZZ99-002\t6", machine_skus=[], acc_skus=[])
        assert rep["columns"]["Qty"] == 1
        assert df["Qty"].tolist() == [4, 6]


class TestQuantities:
    def test_thousands_separator(self):
        df, _ = parse("BOSS25-010\t1,200")
        assert df.iloc[0]["Qty"] == 1200

    def test_trailing_unit_suffix(self):
        df, _ = parse("BOSS25-010\t5 EA")
        assert df.iloc[0]["Qty"] == 5

    def test_float_qty_rounds(self):
        df, rep = parse("BOSS25-010\t2.0\nBOSS70-002\t2.6")
        assert df["Qty"].tolist() == [2, 3]
        assert rep["qty_rounded"] == 1           # 2.0 is exact, 2.6 is not

    def test_zero_and_negative_are_skipped(self):
        df, rep = parse("BOSS25-010\t0\nBOSS70-002\t3")
        assert df["FG SKU"].tolist() == ["BOSS70-002"]
        assert len(rep["skipped"]) == 1
        assert rep["skipped"][0]["line"] == 1

    def test_unreadable_qty_is_skipped_with_a_reason(self):
        df, rep = parse("FG SKU\tQty\nBOSS25-010\tTBD")
        assert df.empty
        assert "isn't a number" in rep["skipped"][0]["reason"]


class TestCleaning:
    def test_lowercase_matches_catalog_casing(self):
        df, rep = parse("boss25-010\t2")
        assert df.iloc[0]["FG SKU"] == "BOSS25-010"
        assert rep["unknown_fg"] == []

    def test_excel_debris_is_stripped(self):
        # Non-breaking space, quotes, and Excel's leading text apostrophe.
        df, _ = parse("\xa0'BOSS25-010'\t\"BOSS25-A016\"\t 5 ")
        assert df.iloc[0].tolist() == ["BOSS25-010", "BOSS25-A016", 5]

    def test_blank_lines_are_dropped(self):
        _, rep = parse("BOSS25-010\t1\n\n\nBOSS70-002\t1\n")
        assert rep["rows"] == 2
        assert rep["skipped"] == []

    def test_formula_injection_row_is_skipped(self):
        df, rep = parse("=cmd|'/c calc'!A1\t5\nBOSS25-010\t2")
        assert df["FG SKU"].tolist() == ["BOSS25-010"]
        assert "=" in rep["skipped"][0]["reason"]

    def test_formula_accessory_is_dropped_but_row_survives(self):
        df, rep = parse("FG SKU\tAccessory\tQty\nBOSS25-010\t@SUM(A1)\t2")
        assert df.iloc[0].tolist() == ["BOSS25-010", "", 2]
        assert rep["dropped_acc"] == 1

    def test_row_without_fg_sku_is_skipped(self):
        df, rep = parse("FG SKU\tQty\n\t5\nBOSS25-010\t1")
        assert df["FG SKU"].tolist() == ["BOSS25-010"]
        assert rep["skipped"][0]["reason"] == "no FG SKU in this row"


class TestDuplicates:
    def test_combined_by_default(self):
        df, rep = parse("BOSS25-010\t2\nBOSS25-010\t3\nBOSS70-002\t1")
        assert df["FG SKU"].tolist() == ["BOSS25-010", "BOSS70-002"]
        assert df["Qty"].tolist() == [5, 1]
        assert rep["merged"] == 1
        assert rep["units"] == 6

    def test_not_combined_when_disabled(self):
        df, rep = parse("BOSS25-010\t2\nBOSS25-010\t3", combine_duplicates=False)
        assert len(df) == 2
        assert rep["merged"] == 0
        assert rep["units"] == 5

    def test_different_accessory_is_a_different_row(self):
        df, _ = parse("BOSS25-010\tBOSS25-A016\t2\nBOSS25-010\t\t3")
        assert len(df) == 2

    def test_dedupe_preserves_first_seen_order(self):
        df, _ = parse("SDG25\t1\nBOSS25-010\t1\nSDG25\t1")
        assert df["FG SKU"].tolist() == ["SDG25", "BOSS25-010"]


class TestUnknownSkus:
    def test_unknown_fg_is_reported_but_kept(self):
        df, rep = parse("BOSS25-010\t1\nNOPE-123\t1")
        assert len(df) == 2
        assert rep["unknown_fg"] == ["NOPE-123"]

    def test_unknown_accessory_is_reported(self):
        _, rep = parse("BOSS25-010\tBOSS25-A999\t1")
        assert rep["unknown_acc"] == ["BOSS25-A999"]

    def test_no_catalog_means_no_unknown_noise(self):
        _, rep = parse("NOPE-123\t1", machine_skus=[], acc_skus=[])
        assert rep["unknown_fg"] == []
        assert rep["unknown_acc"] == []

    def test_unknowns_are_deduped(self):
        _, rep = parse("NOPE-123\t1\nNOPE-123\t2", combine_duplicates=False)
        assert rep["unknown_fg"] == ["NOPE-123"]


class TestEmptyAndJunk:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n", None])
    def test_empty_input_returns_empty_frame(self, text):
        df, rep = parse(text)
        assert df.empty
        assert list(df.columns) == ["FG SKU", "Accessory SKU", "Qty"]
        assert rep["rows"] == 0 and rep["units"] == 0

    def test_only_a_header_row_yields_nothing(self):
        df, rep = parse("FG SKU ID\tBUILD QTY")
        assert df.empty
        assert rep["header"] is True

    def test_prose_paste_is_not_silently_imported_as_skus(self):
        df, _ = parse("please build five more units next week")
        # One line, one column, no digits ⇒ it lands as a single unknown SKU
        # rather than being multiplied into phantom rows.
        assert len(df) <= 1


class TestDownstreamContract:
    def test_output_feeds_build_manual_schedule(self):
        from core.data_loader import build_manual_schedule
        df, _ = parse("BOSS25-010\tBOSS25-A016\t5\nSDG25\t\t2")
        sched = build_manual_schedule(df, location="Henderson",
                                      machine_skus=set(MACHINE))
        assert len(sched) == 2
        assert sched["BUILD QTY"].sum() == 7
        assert set(sched["LOCATION"]) == {"HENDERSON"}
        assert sched.iloc[0]["FG SKU ID"] == "BOSS25-010"

    def test_qty_column_is_integer_typed(self):
        df, _ = parse("BOSS25-010\t5")
        assert pd.api.types.is_integer_dtype(df["Qty"])
