"""``core.uploaded_schedule_storage``: honest row counts, newest save first.

``rows`` used to be a line-break count — a quoted cell with a hard return
(an Excel comment) counted as two rows, so the 📂 picker said "296 rows" for
a 288-row file and the number could never be reconciled with the pill. The
count now parses the CSV. And the listing used to be alphabetical, so the
picker defaulted to "Hen August 07/16" although "Hen August 08/04" was newer.
"""
import io
import json

import pandas as pd
import pytest

import core.uploaded_schedule_storage as us
from core.uploaded_schedule_storage import (
    count_csv_data_rows, names_newest_first, list_uploaded_schedules,
)

HEADER = "LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,CUSTOMER NAME\n"


class TestCountCsvDataRows:
    def test_plain_rows(self):
        text = HEADER + "HENDERSON,A,,1,Jul-26,X\nHENDERSON,B,,2,Jul-26,Y\n"
        assert count_csv_data_rows(text) == 2

    def test_quoted_multi_line_cell_is_one_row(self):
        text = HEADER + 'HENDERSON,A,,1,Jul-26,"note line 1\nnote line 2\nline 3"\nHENDERSON,B,,2,Jul-26,Y\n'
        assert count_csv_data_rows(text) == 2
        # the old way over-counted
        assert us._count_csv_data_rows is count_csv_data_rows
        assert sum(1 for l in text.splitlines() if l.strip()) - 1 == 4

    def test_blank_and_whitespace_lines_are_skipped_like_pandas(self):
        text = HEADER + "HENDERSON,A,,1,Jul-26,X\n\n   \nHENDERSON,B,,2,Jul-26,Y\n\n"
        assert count_csv_data_rows(text) == 2 == len(pd.read_csv(io.StringIO(text)))

    def test_crlf_and_no_trailing_newline(self):
        text = HEADER.replace("\n", "\r\n") + "HENDERSON,A,,1,Jul-26,X\r\nHENDERSON,B,,2,Jul-26,Y"
        assert count_csv_data_rows(text) == 2

    def test_header_only_empty_and_none_like(self):
        assert count_csv_data_rows(HEADER) == 0
        assert count_csv_data_rows("") == 0
        assert count_csv_data_rows("   \n") == 0

    @pytest.mark.parametrize("location", ["Henderson", "Spartanburg", "Cypress"])
    def test_agrees_with_pandas_on_every_bundled_saved_schedule(self, location):
        blocks = us.load_all_uploaded_schedules().get(location) or {}
        if not blocks:
            pytest.skip(f"no saved schedules for {location}")
        for name, block in blocks.items():
            text = block["csv_content"]
            assert count_csv_data_rows(text) == len(pd.read_csv(io.StringIO(text))), name


class TestNamesNewestFirst:
    BLOCKS = {
        "Hen August 07/16": {"saved_at": "2026-07-16T15:11:46-07:00"},
        "Hen August 08/04": {"saved_at": "2026-08-04T10:30:32-07:00"},
        "Hen Jun 05/27":    {"saved_at": "2026-05-27T22:28:52-07:00"},
    }

    def test_newest_save_first_regardless_of_name(self):
        assert names_newest_first(self.BLOCKS) == [
            "Hen August 08/04", "Hen August 07/16", "Hen Jun 05/27",
        ]

    def test_offsets_are_honoured_not_compared_as_text(self):
        blocks = {
            "a": {"saved_at": "2026-08-04T10:30:00-07:00"},   # 17:30 UTC
            "b": {"saved_at": "2026-08-04T12:00:00+00:00"},   # 12:00 UTC — earlier
        }
        assert names_newest_first(blocks) == ["a", "b"]

    def test_undated_and_malformed_blocks_sort_last_then_alphabetical(self):
        blocks = dict(self.BLOCKS)
        blocks["zzz no date"] = {"rows": 1}
        blocks["aaa no date"] = {}
        blocks["mmm bad date"] = {"saved_at": "yesterday"}
        blocks["not a dict"] = "oops"
        order = names_newest_first(blocks)
        assert order[:3] == ["Hen August 08/04", "Hen August 07/16", "Hen Jun 05/27"]
        assert order[3] == "mmm bad date"
        assert order[4:] == ["aaa no date", "not a dict", "zzz no date"]

    def test_ties_are_alphabetical_and_non_dict_input_is_empty(self):
        blocks = {"b": {"saved_at": "2026-08-04T10:30:00"}, "a": {"saved_at": "2026-08-04T10:30:00"}}
        assert names_newest_first(blocks) == ["a", "b"]
        assert names_newest_first(None) == []
        assert names_newest_first([]) == []

    def test_list_uploaded_schedules_uses_it(self, tmp_path, monkeypatch):
        path = tmp_path / "uploaded_schedules.json"
        path.write_text(json.dumps({"Henderson": self.BLOCKS, "Cypress": "junk"}), encoding="utf-8")
        monkeypatch.setattr(us, "SCHEDULES_PATH", path)
        assert list_uploaded_schedules("Henderson") == [
            "Hen August 08/04", "Hen August 07/16", "Hen Jun 05/27",
        ]
        assert list_uploaded_schedules("Cypress") == []
        assert list_uploaded_schedules("Spartanburg") == []


class TestSaveStoresTheParsedCount:
    def test_rows_is_the_parsed_data_row_count(self, monkeypatch):
        pushed = []
        monkeypatch.setattr(us, "_fetch_existing_from_github", lambda token: ({}, None))
        monkeypatch.setattr(
            us, "save_catalog_to_github",
            lambda content, path, token, message, sha: pushed.append(content) or {"commit": {}},
        )
        monkeypatch.setattr(us, "_write_local", lambda content: None)
        text = HEADER + 'HENDERSON,A,,1,Jul-26,"two\nlines"\nHENDERSON,B,,2,Jul-26,Y\n'
        us.save_uploaded_schedule_to_github("Henderson", "Test save", text, "tok")
        block = json.loads(pushed[-1])["Henderson"]["Test save"]
        assert block["rows"] == 2
        assert block["csv_content"] == text
        assert block["saved_at"]
