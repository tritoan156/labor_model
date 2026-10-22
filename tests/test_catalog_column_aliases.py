"""The shared catalog-column alias table and its resolver.

The labor catalogs began as the IE's Excel sheets with headers like
``Warehouse (PICK)`` and ``Final Assy (FN-ASSY)``; the app writes short names
back. ``CATALOG_COLUMN_ALIASES`` is the single place both spellings are known,
so the loaders and the Excel import recognise exactly the same headers — and a
refactor that moved the loaders onto the table must not have changed what
they load.
"""
import pandas as pd
import pytest

from core.data_loader import (
    CATALOG_COLUMN_ALIASES,
    canonical_catalog_column,
    load_acc_labor,
    load_machine_labor,
)

# The IE workbook's headers as documented in the loader (machine) and as the
# accessory loader's patterns were tuned for.
IE_MACHINE_HEADERS = {
    "SKU ID": "SKU",
    "Description": "Description",
    "Warehouse (PICK)": "Warehouse",
    "Wire Assy (PREP-WH)": "Wire",
    "Sub Trl (SUB-TRL)": "Trailer",
    "Final Assy (FN-ASSY)": "FN_Assy",
    "PDI (TEST-PDI)": "PDI",
    "QC (TEST-FN-QC)": "QC",
    "Ship (MAT-REIC-SHIP)": "Ship",
    "🔋Bat": "Bat",
}
IE_ACCESSORY_HEADERS = {
    "SKU ID": "SKU",
    "Description": "Description",
    "Warehouse (PICK)": "Warehouse",
    "Acc KIT (PREP-ACC)": "AccKIT",
    "Battery Assy (PREP-NP)": "Nameplate Prep",
    "Battery Sub raw (SUB-BTR raw)": "BattSubRaw",
    "PM Acc (SUB-PM)": "PMAcc",
    "Gen Sub (SUB-GEN)": "GenAcc",
    "Compressor (SUB-COM)": "ComAcc",
}
# Columns the IE sheet carries that are nothing to the app.
IE_NOISE = ["🟡", "Unit", "Total", "Lead", "Flag", ""]


class TestResolverOnTheIEHeaders:
    @pytest.mark.parametrize("header,expected", list(IE_MACHINE_HEADERS.items()))
    def test_machine(self, header, expected):
        assert canonical_catalog_column(header, "machine") == expected

    @pytest.mark.parametrize("header,expected", list(IE_ACCESSORY_HEADERS.items()))
    def test_accessory(self, header, expected):
        assert canonical_catalog_column(header, "accessory") == expected

    @pytest.mark.parametrize("header", IE_NOISE)
    def test_noise_columns_resolve_to_nothing(self, header):
        assert canonical_catalog_column(header, "machine") is None
        assert canonical_catalog_column(header, "accessory") is None


class TestResolverOnTheAppsOwnHeaders:
    @pytest.mark.parametrize("catalog", ["machine", "accessory"])
    def test_every_canonical_name_resolves_to_itself(self, catalog):
        for canonical in CATALOG_COLUMN_ALIASES[catalog]:
            assert canonical_catalog_column(canonical, catalog) == canonical

    def test_case_and_whitespace_are_ignored(self):
        assert canonical_catalog_column("  fn_assy ", "machine") == "FN_Assy"
        assert canonical_catalog_column("WAREHOUSE", "accessory") == "Warehouse"

    def test_exact_name_beats_a_substring_from_an_earlier_entry(self):
        # "Trailer" also contains nothing from Warehouse/Wire; but "Wire" the
        # exact name must not be captured by a looser earlier pattern either.
        assert canonical_catalog_column("Wire", "machine") == "Wire"


class TestRoutingHeadersAreNeverAliased:
    """``Final Station HND`` contains "Final", which is an FN_Assy alias. The
    import matches routing columns exactly against the live header; the
    resolver must refuse them so a stale live header can't send a routing
    column's cells into the Final-Assembly minutes."""

    @pytest.mark.parametrize("header", [
        "Final Station HND", "Final Station SPB", "Final Station CYP",
        "AccKIT Station HND", "PDI Station CYP", "Wire Station SPB",
        "Undercarriage Station HND", "final station hnd",
    ])
    def test_refused(self, header):
        assert canonical_catalog_column(header, "machine") is None


class TestResolverEdgeCases:
    def test_blank_none_and_unknown_catalog(self):
        assert canonical_catalog_column("", "machine") is None
        assert canonical_catalog_column(None, "machine") is None
        assert canonical_catalog_column("Warehouse", "nope") is None

    def test_line_break_inside_a_header_is_tolerated(self):
        assert canonical_catalog_column("Warehouse\n(PICK)", "machine") == "Warehouse"


class TestLoadersStillLoadTheRealCatalogsUnchanged:
    """The loaders now read their patterns from the table. Load-bearing: the
    deployed CSVs must come out with the same columns and values as before."""

    def test_machine_catalog_columns_and_a_known_row(self):
        df = load_machine_labor()
        for col in ("SKU", "Description", "Warehouse", "Wire", "Trailer", "FN_Assy",
                    "PDI", "QC", "Ship", "ETO", "Bat", "Final Station HND"):
            assert col in df.columns
        assert len(df) > 100
        # A placeholder this conversation added, with known median times.
        row = df.loc["SDG220S XXX"]
        assert float(row["Trailer"]) == 240.0 and float(row["FN_Assy"]) == 45.0

    def test_accessory_catalog_columns_and_a_known_row(self):
        df = load_acc_labor()
        for col in ("SKU", "Description", "Warehouse", "AccKIT", "Nameplate Prep",
                    "BattSubRaw", "PMAcc", "GenAcc", "ComAcc"):
            assert col in df.columns
        assert len(df) > 100
        assert float(df.loc["SDG220S AXXX", "GenAcc"]) == 335.0

    def test_ie_header_spelling_still_loads_through_the_loader(self, tmp_path):
        # The loader's own tolerance for the verbose headers must survive the
        # refactor — this is the spelling machine_clean.csv was first exported in.
        p = tmp_path / "m.csv"
        pd.DataFrame([{
            "🟡": "", "Unit": "", "SKU ID": "T-1", "Description": "test",
            "Warehouse (PICK)": 5, "Wire Assy (PREP-WH)": 10, "Sub Trl (SUB-TRL)": 90,
            "Final Assy (FN-ASSY)": 45, "PDI (TEST-PDI)": 30, "QC (TEST-FN-QC)": 30,
            "Ship (MAT-REIC-SHIP)": 20, "Total": 230, "🔋Bat": 1, "Lead": "", "Flag": "",
        }]).to_csv(p, index=False)
        df = load_machine_labor(p)
        r = df.loc["T-1"]
        assert (r["Warehouse"], r["Wire"], r["Trailer"], r["FN_Assy"]) == (5, 10, 90, 45)
        assert (r["PDI"], r["QC"], r["Ship"], r["Bat"]) == (30, 30, 20, 1)
