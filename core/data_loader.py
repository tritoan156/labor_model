"""Data loaders for Labor Capacity Model (Henderson, Spartanburg, Cypress).

Reads CSV inputs and returns clean DataFrames:
  - load_machine_labor() → machine SKU labor table
  - load_acc_labor()     → accessory SKU labor table
  - load_schedule()      → work order schedule (auto-detects month, any location)
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Union
import pandas as pd

from .constants import CUSTOMER_SUFFIXES, BATTERY_COUNT_OVERRIDES

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _safe_get(df: pd.DataFrame, possible_columns: list, default=0):
    """Return the first column name from possible_columns that exists in df."""
    for col in possible_columns:
        if col in df.columns:
            return col
    return None


def load_machine_labor(path: Path | str | None = None) -> pd.DataFrame:
    """Load Machine_Clean CSV → DataFrame indexed by SKU.

    Columns returned (one row per SKU): Warehouse, Wire, Trailer, FN_Assy_old,
    PDI, QC, Ship, Bat, Description.
    """
    if path is None:
        path = DATA_DIR / "machine_clean.csv"
    df = pd.read_csv(path)
    # Strip whitespace on string columns
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].astype(str).str.strip()
    # Standardise column names — the CSV may have header rows like 'Warehouse\n(PICK)'.
    # Rename them to friendly internals.
    rename_map = {}
    for c in df.columns:
        s = str(c).replace("\n", " ").replace("(PICK)", "").strip()
        rename_map[c] = s
    df = df.rename(columns=rename_map)

    # Identify columns by pattern. The original CSV header is something like:
    # 🟡, Unit, SKU ID, Description, Warehouse (PICK), Wire Assy (PREP-WH),
    # Sub Trl (SUB-TRL), Final Assy (FN-ASSY), PDI (TEST-PDI), QC (TEST-FN-QC),
    # Ship (MAT-REIC-SHIP), Total, 🔋Bat, Lead, Flag
    sku_col = _safe_get(df, ["SKU ID", "SKU", "FG SKU"])
    desc_col = _safe_get(df, ["Description"])
    bat_col = _safe_get(df, ["🔋Bat", "Bat", "Battery"])

    pick_col = next((c for c in df.columns if "PICK" in str(c).upper() or c.startswith("Warehouse")), None)
    wire_col = next((c for c in df.columns if "PREP-WH" in str(c).upper() or "Wire" in str(c)), None)
    trl_col  = next((c for c in df.columns if "SUB-TRL" in str(c).upper() or "Trl" in str(c) or "Trailer" in str(c)), None)
    fn_col   = next((c for c in df.columns if "FN-ASSY" in str(c).upper() or "Final" in str(c)), None)
    pdi_col  = next((c for c in df.columns if "TEST-PDI" in str(c).upper() or c.startswith("PDI")), None)
    qc_col   = next((c for c in df.columns if "FN-QC" in str(c).upper() or c.startswith("QC")), None)
    ship_col = next((c for c in df.columns if "SHIP" in str(c).upper()), None)

    out = pd.DataFrame()
    out["SKU"] = df[sku_col]
    out["Description"] = df[desc_col].fillna("") if desc_col else ""
    out["Warehouse"] = pd.to_numeric(df[pick_col], errors="coerce").fillna(0) if pick_col else 0
    out["Wire"] = pd.to_numeric(df[wire_col], errors="coerce").fillna(0) if wire_col else 0
    out["Trailer"] = pd.to_numeric(df[trl_col], errors="coerce").fillna(0) if trl_col else 0
    out["FN_Assy_old"] = pd.to_numeric(df[fn_col], errors="coerce").fillna(0) if fn_col else 0
    out["PDI"] = pd.to_numeric(df[pdi_col], errors="coerce").fillna(0) if pdi_col else 0
    out["QC"] = pd.to_numeric(df[qc_col], errors="coerce").fillna(0) if qc_col else 0
    out["Ship"] = pd.to_numeric(df[ship_col], errors="coerce").fillna(0) if ship_col else 0
    out["Bat"] = pd.to_numeric(df[bat_col], errors="coerce").fillna(0).astype(int) if bat_col else 0

    # Apply battery-count overrides
    for sku, count in BATTERY_COUNT_OVERRIDES.items():
        out.loc[out["SKU"] == sku, "Bat"] = count

    out = out[out["SKU"].notna() & (out["SKU"] != "")]
    out = out.set_index("SKU", drop=False)
    return out


def load_acc_labor(path: Path | str | None = None) -> pd.DataFrame:
    """Load Acc_Clean CSV → DataFrame indexed by SKU.

    Returns columns: Warehouse, AccKIT, Nameplate Prep, BattSubRaw, PMAcc, GenAcc,
    Compressor, Description.
    """
    if path is None:
        path = DATA_DIR / "acc_clean.csv"
    df = pd.read_csv(path)
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].astype(str).str.strip()
    rename_map = {c: str(c).replace("\n", " ") for c in df.columns}
    df = df.rename(columns=rename_map)

    sku_col = _safe_get(df, ["SKU ID", "SKU"])
    desc_col = _safe_get(df, ["Description"])
    pick_col = next((c for c in df.columns if "PICK" in str(c).upper() or c.startswith("Warehouse")), None)
    kit_col = next((c for c in df.columns if "PREP-ACC" in str(c).upper() or "Acc KIT" in str(c)), None)
    prep_col = next((c for c in df.columns if "PREP-NP" in str(c).upper() or "Battery Assy" in str(c)), None)
    btr_col = next((c for c in df.columns if "SUB-BTR raw" in str(c) or "SUB-BTR)" in str(c) or "Battery Sub raw" in str(c)), None)
    pm_col = next((c for c in df.columns if "SUB-PM" in str(c).upper() or c.startswith("PM ")), None)
    gen_col = next((c for c in df.columns if "SUB-GEN" in str(c).upper() or "Gen Sub" in str(c)), None)
    com_col = next((c for c in df.columns if "SUB-COM" in str(c).upper() or "Compressor" in str(c)), None)

    out = pd.DataFrame()
    out["SKU"] = df[sku_col]
    out["Description"] = df[desc_col].fillna("") if desc_col else ""
    out["Warehouse"] = pd.to_numeric(df[pick_col], errors="coerce").fillna(0) if pick_col else 0
    out["AccKIT"] = pd.to_numeric(df[kit_col], errors="coerce").fillna(0) if kit_col else 0
    out["Nameplate Prep"] = pd.to_numeric(df[prep_col], errors="coerce").fillna(0) if prep_col else 0
    out["BattSubRaw"] = pd.to_numeric(df[btr_col], errors="coerce").fillna(0) if btr_col else 0
    out["PMAcc"] = pd.to_numeric(df[pm_col], errors="coerce").fillna(0) if pm_col else 0
    out["GenAcc"] = pd.to_numeric(df[gen_col], errors="coerce").fillna(0) if gen_col else 0
    out["Compressor"] = pd.to_numeric(df[com_col], errors="coerce").fillna(0) if com_col else 0

    out = out[out["SKU"].notna() & (out["SKU"] != "")]
    out = out.set_index("SKU", drop=False)
    return out


def collapse_customer_suffix(sku: str, machine_skus: set) -> tuple[str, str]:
    """Return (base_sku, decal_suffix) — strip customer suffix if present.

    A suffix is only stripped if the resulting base SKU exists in the catalog.
    """
    s = str(sku).strip()
    for sfx in CUSTOMER_SUFFIXES:
        if s.endswith(sfx):
            base = s[:-len(sfx)]
            if base in machine_skus:
                return base, sfx
    return s, ""


def _detect_schedule_months(df: pd.DataFrame, include_carryover: bool) -> list[str]:
    """Return the production month values to keep.

    The "current" month uses format Mon-YY (e.g. "May-26").
    The carryover month uses format YY-Mon (e.g. "26-Apr").
    We detect them from the data rather than hard-coding.
    """
    all_months = df["PRODUCTION MONTH"].dropna().unique().tolist()

    # Carryover rows match YY-Mon pattern (e.g. "26-Apr")
    carryover_pattern = re.compile(r"^\d{2}-[A-Za-z]{3}$")
    carryover = [m for m in all_months if carryover_pattern.match(str(m))]

    # Current rows match Mon-YY pattern (e.g. "May-26")
    current_pattern = re.compile(r"^[A-Za-z]{3}-\d{2}$")
    current = [m for m in all_months if current_pattern.match(str(m))]

    keep = list(current)
    if include_carryover:
        keep.extend(carryover)
    return keep


def load_schedule(
    path: Union[Path, str, io.IOBase, None] = None,
    location: str = "HENDERSON",
    include_carryover: bool = True,
    machine_skus: set | None = None,
) -> pd.DataFrame:
    """Load production schedule CSV → DataFrame.

    `path` can be a file path (Path/str), a file-like object (e.g. io.BytesIO
    from a Streamlit upload), or None to use the bundled May 2026 schedule.

    Filters to the given location. Month filtering is auto-detected from the
    data so any schedule month works without code changes.
    Strips customer suffixes (HRC/UR/ES/HERC) when machine_skus is provided.
    """
    if path is None:
        path = DATA_DIR / "may_schedule.csv"
    df = pd.read_csv(path, encoding="latin-1")
    # Drop empty/junk rows
    df = df[df["FG SKU ID"].notna() & df["BUILD QTY"].notna()].copy()
    # Normalise text cols
    df["LOC"] = df["LOCATION"].astype(str).str.upper().str.strip()
    df["FG_RAW"] = df["FG SKU ID"].astype(str).str.strip()
    df["ACC"] = df["FG ACCRY SKU ID"].astype(str).str.strip().replace("nan", "")
    df["BUILD QTY"] = df["BUILD QTY"].astype(int)

    # Filter location first so month detection is scoped to the right rows
    if location:
        df = df[df["LOC"] == location.upper()]

    # Detect carryover rows (YY-Mon format) vs current rows (Mon-YY format)
    carryover_pattern = re.compile(r"^\d{2}-[A-Za-z]{3}$")
    df["CARRYOVER"] = df["PRODUCTION MONTH"].astype(str).apply(
        lambda m: bool(carryover_pattern.match(m))
    )

    # Filter to detected months
    months = _detect_schedule_months(df, include_carryover)
    if months:
        df = df[df["PRODUCTION MONTH"].isin(months)]

    # Apply customer-suffix collapse if we have a catalog
    if machine_skus is not None:
        decals = df["FG_RAW"].apply(lambda s: collapse_customer_suffix(s, machine_skus))
        df["FG_BASE"] = decals.apply(lambda x: x[0])
        df["DECAL"] = decals.apply(lambda x: x[1])
    else:
        df["FG_BASE"] = df["FG_RAW"]
        df["DECAL"] = ""

    # Sort: carryover first
    df = df.sort_values(by=["CARRYOVER"], ascending=False).reset_index(drop=True)
    return df


def build_manual_schedule(
    entries: pd.DataFrame,
    location: str,
    machine_skus: set | None = None,
) -> pd.DataFrame:
    """Build a schedule DataFrame from manual SKU entries (no CSV upload).

    `entries` is a DataFrame with columns: FG SKU, Accessory SKU, Qty.
    Returns the same schema as `load_schedule()` so the downstream pipeline
    (`expand_schedule`, `build_capacity_table`, etc.) works unchanged.

    Rows with empty FG SKU or Qty <= 0 are skipped.
    """
    rows = []
    for _, r in entries.iterrows():
        fg = str(r.get("FG SKU", "") or "").strip()
        acc = str(r.get("Accessory SKU", "") or "").strip()
        try:
            qty = int(r.get("Qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        if not fg or qty <= 0:
            continue
        rows.append({
            "LOCATION": location.upper(),
            "PRODUCTION MONTH": "Manual",
            "FG SKU ID": fg,
            "FG ACCRY SKU ID": acc,
            "BUILD QTY": qty,
            "LOC": location.upper(),
            "FG_RAW": fg,
            "ACC": acc,
            "CARRYOVER": False,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Apply customer-suffix collapse to match the CSV pipeline
    if machine_skus is not None:
        decals = df["FG_RAW"].apply(lambda s: collapse_customer_suffix(s, machine_skus))
        df["FG_BASE"] = decals.apply(lambda x: x[0])
        df["DECAL"] = decals.apply(lambda x: x[1])
    else:
        df["FG_BASE"] = df["FG_RAW"]
        df["DECAL"] = ""

    return df.reset_index(drop=True)
