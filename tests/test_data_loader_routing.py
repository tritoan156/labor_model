"""Tests for per-facility Undercarriage Station routing in
core.data_loader.load_machine_labor.

The machine catalog carries per-facility routing columns
``Undercarriage Station {HND/SPB/CYP}`` (alongside Final / AccKIT / PDI / Wire).
The loader must surface them, default a missing/blank/unknown value to
``Undercarriage``, and preserve valid station keys verbatim.
"""
import pandas as pd

from core.data_loader import load_machine_labor

BASE_COLS = ["SKU", "Description", "Warehouse", "Wire", "Trailer", "FN_Assy",
             "PDI", "QC", "Ship", "ETO", "Bat"]
UC_COLS = ["Undercarriage Station HND", "Undercarriage Station SPB",
           "Undercarriage Station CYP"]


def _write(tmp_path, df):
    p = tmp_path / "machine.csv"
    df.to_csv(p, index=False)
    return p


def test_undercarriage_columns_surface_with_values(tmp_path):
    df = pd.DataFrame([
        dict(SKU="PDS100-001", Description="x", FN_Assy=118,
             **{"Undercarriage Station HND": "ComAcc",
                "Undercarriage Station SPB": "Undercarriage",
                "Undercarriage Station CYP": "Undercarriage"}),
    ])
    out = load_machine_labor(_write(tmp_path, df))
    row = out.loc["PDS100-001"]
    assert row["Undercarriage Station HND"] == "ComAcc"
    assert row["Undercarriage Station SPB"] == "Undercarriage"
    assert row["Undercarriage Station CYP"] == "Undercarriage"


def test_missing_columns_default_to_undercarriage(tmp_path):
    # A pre-migration CSV without the columns still loads; the loader emits them
    # defaulted to "Undercarriage" so compute never crashes.
    df = pd.DataFrame([dict(SKU="SDG25-001", Description="y", Trailer=45)])
    out = load_machine_labor(_write(tmp_path, df))
    for c in UC_COLS:
        assert c in out.columns
        assert out.loc["SDG25-001", c] == "Undercarriage"


def test_blank_and_unknown_values_fall_back(tmp_path):
    df = pd.DataFrame([
        dict(SKU="A", Description="", **{"Undercarriage Station HND": "",
                                         "Undercarriage Station SPB": "NotAStation",
                                         "Undercarriage Station CYP": "ComAcc"}),
    ])
    out = load_machine_labor(_write(tmp_path, df))
    row = out.loc["A"]
    assert row["Undercarriage Station HND"] == "Undercarriage"   # blank → default
    assert row["Undercarriage Station SPB"] == "Undercarriage"   # unknown → default
    assert row["Undercarriage Station CYP"] == "ComAcc"          # valid → kept
