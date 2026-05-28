"""Data loaders for Labor Capacity Model (Henderson, Spartanburg, Cypress).

Reads CSV inputs and returns clean DataFrames:
  - load_machine_labor()    → machine SKU labor table
  - load_acc_labor()        → accessory SKU labor table
  - load_schedule()         → work order schedule (auto-detects month, any location)
  - load_item_master()      → unified items table (defaults + family variants + per-accessory)
  - load_item_packages()    → bundled packages (e.g. CWP, AWP)
  - resolve_item_time()     → family-aware lookup for one (item, family, side)
  - unique_abbrs()          → unique item abbreviations from the master
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Union
import pandas as pd

# ---------------------------------------------------------------------------
# Compatibility shim — newer pandas (the one Streamlit Cloud's Python 3.14
# image ships with) defaults string columns to a PyArrow-backed dtype. That
# causes two distinct breakages in this codebase:
#   1. .apply(lambda) on the column may leak Arrow <NA> scalars into pure-
#      Python callbacks like re.match, raising TypeError.
#   2. Arithmetic on a column the loader *thought* it had coerced to numeric
#      (`pd.to_numeric(...)` can still preserve the Arrow backend in newer
#      pandas) raises "Can only string multiply by an integer" when the
#      result is multiplied by another Series.
# Switching the future option off restores the old object-dtype behavior so
# every loader + arithmetic op in the project behaves like it always has.
# Guarded with try/except so older pandas (where the option doesn't exist)
# silently keeps working.
# ---------------------------------------------------------------------------
try:
    pd.set_option("future.infer_string", False)
except Exception:
    pass
try:
    pd.set_option("mode.string_storage", "python")
except Exception:
    pass

from .constants import CUSTOMER_SUFFIXES, week_of_month_series

__all__ = [
    "load_machine_labor",
    "load_acc_labor",
    "load_schedule",
    "build_manual_schedule",
    "load_item_master",
    "load_item_packages",
    "resolve_item_time",
    "unique_abbrs",
    "collapse_customer_suffix",
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Column-name fuzzy matcher (shared across loaders)
# ---------------------------------------------------------------------------
def _find_column(df_cols, *patterns: str) -> str | None:
    """Case-insensitive substring match: return the first DataFrame column
    whose header contains any of the given ``patterns`` (themselves
    case-insensitive).

    The match is robust to whitespace, line breaks, and prefixes/suffixes the
    user's ERP export might add — e.g. ``"PRODUCTION MONTH"`` matches a header
    cell named ``"Production Month "``, ``"PROD\\nMONTH"`` or ``"production_month"``.
    """
    norm_cols = [(c, str(c).strip().upper().replace("\n", " ")) for c in df_cols]
    for pat in patterns:
        pat_norm = pat.strip().upper().replace("\n", " ")
        for orig, norm in norm_cols:
            if pat_norm in norm:
                return orig
    return None


class ScheduleColumnError(ValueError):
    """Raised by ``load_schedule`` when a required column can't be matched.

    The exception's ``args[0]`` is a human-friendly message that the upload
    UI surfaces verbatim (no token / no internals leak).
    """


def _safe_get(df: pd.DataFrame, possible_columns: list, default=0):
    """Return the first column name from possible_columns that exists in df."""
    for col in possible_columns:
        if col in df.columns:
            return col
    return None


def load_machine_labor(path: Path | str | None = None) -> pd.DataFrame:
    """Load Machine_Clean CSV → DataFrame indexed by SKU.

    Columns returned (one row per SKU): Warehouse, Wire, Trailer, FN_Assy,
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

    # Robust detection — match original verbose headers OR friendly names
    # written back by the catalog save (e.g. "Wire", "Trailer", "FN_Assy").
    def _find(df_cols, *patterns):
        for c in df_cols:
            s = str(c).upper()
            for p in patterns:
                if p.upper() in s:
                    return c
        return None

    pick_col = _find(df.columns, "PICK", "Warehouse")
    wire_col = _find(df.columns, "PREP-WH", "Wire")
    trl_col  = _find(df.columns, "SUB-TRL", "Trl", "Trailer")
    fn_col   = _find(df.columns, "FN-ASSY", "Final", "FN_Assy", "FNAssy", "FN_Assy_old")
    pdi_col  = _find(df.columns, "TEST-PDI", "PDI")
    qc_col   = _find(df.columns, "FN-QC", "QC")
    ship_col = _find(df.columns, "SHIP")
    eto_col  = _find(df.columns, "ETO")

    out = pd.DataFrame()
    out["SKU"] = df[sku_col]
    out["Description"] = df[desc_col].fillna("") if desc_col else ""
    n = len(df)
    zeros = pd.Series([0.0] * n, index=df.index, dtype=float)
    int_zeros = pd.Series([0] * n, index=df.index, dtype=int)
    out["Warehouse"] = pd.to_numeric(df[pick_col], errors="coerce").fillna(0) if pick_col else zeros
    out["Wire"] = pd.to_numeric(df[wire_col], errors="coerce").fillna(0) if wire_col else zeros
    out["Trailer"] = pd.to_numeric(df[trl_col], errors="coerce").fillna(0) if trl_col else zeros
    out["FN_Assy"] = pd.to_numeric(df[fn_col], errors="coerce").fillna(0) if fn_col else zeros
    out["PDI"] = pd.to_numeric(df[pdi_col], errors="coerce").fillna(0) if pdi_col else zeros
    out["QC"] = pd.to_numeric(df[qc_col], errors="coerce").fillna(0) if qc_col else zeros
    out["Ship"] = pd.to_numeric(df[ship_col], errors="coerce").fillna(0) if ship_col else zeros
    out["ETO"] = pd.to_numeric(df[eto_col], errors="coerce").fillna(0) if eto_col else zeros
    out["Bat"] = pd.to_numeric(df[bat_col], errors="coerce").fillna(0).astype(int) if bat_col else int_zeros
    # Last-modified date — empty for rows never edited through the app.
    mod_col = _find(df.columns, "Last Modified", "Modified", "Updated")
    out["Last Modified"] = df[mod_col].fillna("").astype(str) if mod_col else pd.Series([""] * n, index=df.index, dtype=object)

    out = out[out["SKU"].notna() & (out["SKU"] != "")]
    out = out.set_index("SKU", drop=False)
    return out


ITEM_MASTER_COLUMNS = [
    "Abbr", "Description", "FG family", "Accessory SKU",
    "Time on Compressor (min)", "Time on Generator (min)", "Time on PM (min)",
    "Notes",
]


def load_item_master(path: Path | str | None = None) -> pd.DataFrame:
    """Load the unified item master (defaults + family-specific variants).

    Each row is identified by (Abbr, FG family). A blank FG family means the
    row is the default for that item; a non-blank FG family is a variant that
    overrides the default when the accessory's FG family starts with that value.

    Columns: Abbr, Description, FG family, Time on Compressor (min),
    Time on Generator (min), Time on PM (min), Notes.
    """
    if path is None:
        path = DATA_DIR / "item_master.csv"
    if not Path(path).exists():
        return pd.DataFrame(columns=ITEM_MASTER_COLUMNS)
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=ITEM_MASTER_COLUMNS)
    # Backfill columns that may be missing from older CSV versions
    for c in ITEM_MASTER_COLUMNS:
        if c not in df.columns:
            df[c] = 0 if "Time" in c else ""
    # Force text columns to clean strings (handles NaN, mixed dtypes, etc.)
    for c in ("Abbr", "Description", "FG family", "Accessory SKU", "Notes"):
        df[c] = df[c].map(_to_clean_str)
    for c in ("Time on Compressor (min)", "Time on Generator (min)", "Time on PM (min)"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df[ITEM_MASTER_COLUMNS].reset_index(drop=True)


def resolve_item_time(abbr: str, family: str, side: str,
                       master_df: pd.DataFrame) -> float:
    """Return the install time for one item on one accessory.

    Lookup precedence within the single master table:
      1. Variant rows: same Abbr, non-empty FG family that is a prefix of `family`
         (longest matching prefix wins, so SDG125 beats SDG when both are present)
      2. Default row: same Abbr, empty FG family

    All matches are case-insensitive on Abbr and FG family.
    `side` is one of "Compressor", "Generator", "PM" (case-insensitive).
    Returns 0.0 if nothing matches.
    """
    if not abbr or master_df is None or master_df.empty:
        return 0.0
    abbr_norm = str(abbr).strip().upper()
    family_norm = str(family or "").strip().upper()
    side_norm = str(side or "").strip().capitalize()

    col_map = {
        "Compressor": "Time on Compressor (min)",
        "Generator": "Time on Generator (min)",
        "Pm": "Time on PM (min)",
    }
    col = col_map.get(side_norm)
    if col is None or col not in master_df.columns:
        return 0.0

    m = master_df.copy()
    # Force string dtype explicitly — handles cases where pandas inferred the
    # FG family column as float64 (all-NaN) or object with mixed numeric / NaN.
    m["__abbr"] = m["Abbr"].map(_to_clean_str).str.upper()
    m["__family"] = m["FG family"].map(_to_clean_str).str.upper()
    m_abbr = m[m["__abbr"] == abbr_norm]
    if m_abbr.empty:
        return 0.0

    # 1) Best (longest-prefix) variant match
    best_prefix_len = -1
    best_value = None
    for _, row in m_abbr.iterrows():
        fam = row["__family"]
        if not isinstance(fam, str) or not fam:
            continue
        if family_norm.startswith(fam) and len(fam) > best_prefix_len:
            best_prefix_len = len(fam)
            try:
                best_value = float(row[col])
            except (TypeError, ValueError):
                best_value = None
    if best_value is not None:
        return best_value

    # 2) Default row (FG family blank)
    defaults = m_abbr[m_abbr["__family"] == ""]
    if defaults.empty:
        return 0.0
    try:
        return float(defaults.iloc[0][col])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _to_clean_str(value) -> str:
    """Coerce any value (NaN, None, number, etc.) to a clean stripped string.

    NaN / None / "nan" / "None" all collapse to "".
    """
    if value is None:
        return ""
    # numpy NaN is a float — pd.isna handles both
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if s.lower() in ("nan", "none"):
        return ""
    return s


def unique_abbrs(master_df: pd.DataFrame) -> list:
    """Return the unique item abbreviations in the master, preserving first-seen order."""
    if master_df is None or master_df.empty or "Abbr" not in master_df.columns:
        return []
    seen = []
    seen_set = set()
    for a in master_df["Abbr"]:
        s = str(a).strip()
        if s and s.upper() not in seen_set:
            seen_set.add(s.upper())
            seen.append(s)
    return seen


def load_item_packages(path: Path | str | None = None) -> pd.DataFrame:
    """Load the reference table of installable packages (item bundles).

    Returns columns: Package, Description, Total Time (min), Components, Notes.
    Components is a comma-separated list of item abbreviations.
    """
    if path is None:
        path = DATA_DIR / "item_packages.csv"
    if not Path(path).exists():
        return pd.DataFrame(columns=[
            "Package", "Description", "Total Time (min)", "Components", "Notes",
        ])
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=[
            "Package", "Description", "Total Time (min)", "Components", "Notes",
        ])
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].astype(str).str.strip()
    if "Total Time (min)" in df.columns:
        df["Total Time (min)"] = pd.to_numeric(df["Total Time (min)"], errors="coerce").fillna(0)
    return df.reset_index(drop=True)


def load_acc_labor(path: Path | str | None = None) -> pd.DataFrame:
    """Load Acc_Clean CSV → DataFrame indexed by SKU.

    Returns columns: Warehouse, AccKIT, Nameplate Prep, BattSubRaw, PMAcc, GenAcc,
    Compressor, Description. These aggregates are the source of truth for
    capacity calculations — the per-item breakdown (accessory_items.csv) is
    used only for analysis and reconciliation.
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

    # Column detection recognises BOTH the original CSV header format
    # (e.g. "Acc KIT (PREP-ACC)") AND the friendly internal name written
    # back by the catalog save (e.g. "AccKIT", "Nameplate Prep"). This keeps
    # the loader robust whether the CSV was hand-authored or app-saved.
    def _find(df_cols, *patterns):
        for c in df_cols:
            s = str(c).upper()
            for p in patterns:
                if p.upper() in s:
                    return c
        return None

    pick_col = _find(df.columns, "PICK", "Warehouse")
    kit_col  = _find(df.columns, "PREP-ACC", "Acc KIT", "AccKIT")
    prep_col = _find(df.columns, "PREP-NP", "Battery Assy", "Nameplate Prep", "Nameplate")
    btr_col  = _find(df.columns, "SUB-BTR raw", "SUB-BTR)", "Battery Sub raw", "BattSubRaw", "SubRaw")
    pm_col   = _find(df.columns, "SUB-PM", "PMAcc", "PM Acc", "PM ")
    gen_col  = _find(df.columns, "SUB-GEN", "Gen Sub", "GenAcc", "Gen Acc")
    # Prefer an exact "ComAcc" column when present. The catalog editor save AND
    # the reconciliation Apply both write the Compressor-side aggregate to a
    # column named "ComAcc". A leftover legacy "Compressor" column must NOT
    # shadow it — _find() scans columns in file order, so a "Compressor" that
    # sits before "ComAcc" used to win, which meant saved Compressor-side
    # values silently never took effect on reload.
    com_col  = "ComAcc" if "ComAcc" in df.columns else _find(
        df.columns, "SUB-COM", "Compressor", "ComAcc",
    )

    out = pd.DataFrame()
    out["SKU"] = df[sku_col]
    out["Description"] = df[desc_col].fillna("") if desc_col else ""
    # Use Series (not scalar) so columns always exist even if no rows
    n = len(df)
    zeros = pd.Series([0] * n, index=df.index, dtype=float)
    out["Warehouse"] = pd.to_numeric(df[pick_col], errors="coerce").fillna(0) if pick_col else zeros
    out["AccKIT"] = pd.to_numeric(df[kit_col], errors="coerce").fillna(0) if kit_col else zeros
    out["Nameplate Prep"] = pd.to_numeric(df[prep_col], errors="coerce").fillna(0) if prep_col else zeros
    out["BattSubRaw"] = pd.to_numeric(df[btr_col], errors="coerce").fillna(0) if btr_col else zeros
    out["PMAcc"] = pd.to_numeric(df[pm_col], errors="coerce").fillna(0) if pm_col else zeros
    out["GenAcc"] = pd.to_numeric(df[gen_col], errors="coerce").fillna(0) if gen_col else zeros
    out["ComAcc"] = pd.to_numeric(df[com_col], errors="coerce").fillna(0) if com_col else zeros
    # Last-modified date — empty for rows never edited through the app.
    mod_col = _find(df.columns, "Last Modified", "Modified", "Updated")
    out["Last Modified"] = df[mod_col].fillna("").astype(str) if mod_col else pd.Series([""] * len(df), index=df.index, dtype=object)

    out = out[out["SKU"].notna() & (out["SKU"] != "")]
    out = out.set_index("SKU", drop=False)
    # NOTE: We intentionally do NOT override the aggregate values with the
    # item-level sums. The acc_clean.csv aggregate is the source of truth for
    # calculations; the items file is used purely for analysis / reconciliation.
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


_REQUIRED_SCHEDULE_COLUMNS = [
    # (canonical_name, [acceptable patterns for fuzzy matching], friendly label)
    ("LOCATION",         ["LOCATION", "FACILITY", "SITE", "PLANT"],                              "Location"),
    ("FG SKU ID",        ["FG SKU ID", "FG SKU", "FG_SKU", "FINISHED GOOD", "SKU ID"],           "FG SKU ID"),
    ("FG ACCRY SKU ID",  ["FG ACCRY", "FG ACC SKU", "ACCESSORY SKU", "ACCRY SKU", "ACC SKU"],    "Accessory SKU ID"),
    ("BUILD QTY",        ["BUILD QTY", "BUILD QUANTITY", "BUILD_QTY", "QUANTITY", "QTY"],        "Build Qty"),
    ("PRODUCTION MONTH", ["PRODUCTION MONTH", "PROD MONTH", "PRODUCTION DATE", "MONTH"],         "Production Month"),
]
_OPTIONAL_SCHEDULE_COLUMNS = [
    ("CUSTOMER NAME",  ["CUSTOMER NAME", "CUSTOMER", "CUST"]),
    # Day-level scheduling — when present we expose a Week selector in the
    # sidebar so the planner can see "this week" vs "remaining weeks". Absent
    # ⇒ the analysis collapses back to the whole-month behavior, unchanged.
    # PRODUCTION DAY is the **build day** for a unit. The patterns are
    # specific on purpose — a bare "DATE" pattern used to greedy-match
    # unrelated columns like PACKET RELEASE DATE / PLANNED COMPLETION DATE /
    # SHIPPED DATE, so a CSV without a real build-day column would get those
    # picked up as production days and then surface as "rows dated outside
    # the plan month" on the Calendar.
    ("PRODUCTION DAY", ["PRODUCTION DAY", "PROD DAY", "BUILD DAY", "BUILD DATE"]),
]


def _resolve_schedule_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename the user's columns to the canonical names the rest of the loader
    expects. Raises ``ScheduleColumnError`` with a clear, actionable message
    if a required column can't be matched (case-insensitive substring).

    Extra columns are preserved untouched — pandas treats them as ignored.
    """
    rename_map: dict[str, str] = {}
    for canonical, patterns, label in _REQUIRED_SCHEDULE_COLUMNS:
        if canonical in df.columns:
            continue  # already named correctly
        found = _find_column(df.columns, *patterns)
        if found is None:
            tried = ", ".join(f"`{p}`" for p in patterns)
            raise ScheduleColumnError(
                f"Your CSV is missing a column for **{label}**. "
                f"We tried matching: {tried}. "
                "Rename or add this column and re-upload."
            )
        if found != canonical:
            rename_map[found] = canonical
    for canonical, patterns in _OPTIONAL_SCHEDULE_COLUMNS:
        if canonical in df.columns:
            continue
        found = _find_column(df.columns, *patterns)
        if found is not None and found != canonical:
            rename_map[found] = canonical
    return df.rename(columns=rename_map) if rename_map else df


def _coerce_production_month(s: pd.Series) -> pd.Series:
    """Normalize a `PRODUCTION MONTH` column to the canonical ``Mon-YY``
    format (e.g. ``May-26``) where possible.

    Accepts the original formats (``Mon-YY``, ``YY-Mon``) plus US-slash
    (``5/8/2026``), ISO (``2026-05-08``), and any value pandas can coerce
    via ``pd.to_datetime``. Unrecognized values pass through unchanged so
    the planner can still see the literal text in error contexts.

    Carryover rows (``YY-Mon``) are intentionally left alone so the
    downstream ``CARRYOVER`` flag still fires correctly.
    """
    s = s.astype("object").fillna("").astype(str).str.strip()

    carryover_re = re.compile(r"^\d{2}-[A-Za-z]{3}$")
    current_re = re.compile(r"^[A-Za-z]{3}-\d{2}$")

    def _normalize(val: str) -> str:
        if not val:
            return ""
        if carryover_re.match(val) or current_re.match(val):
            return val  # already canonical
        # Try parsing as a date — pd.to_datetime accepts most common formats.
        ts = pd.to_datetime(val, errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%b-%y")
        return val  # leave as-is; downstream filter will surface the issue

    return s.map(_normalize)


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

    Robust to real-world Excel exports:
    - Case-insensitive substring matching on the 5 required column names so
      ``"Location"``, ``"FG SKU"``, ``"Qty"`` etc. all resolve.
    - Strips UTF-8 BOM (Excel 2016+ Windows default) from column headers.
    - Accepts ``Mon-YY`` / ``YY-Mon`` / US-slash / ISO / pandas-parseable
      production-month formats.
    - Drops rows with ``BUILD QTY <= 0`` so zero-qty phantoms can't sneak
      through.

    Raises ``ScheduleColumnError`` (a ValueError subclass) with a planner-
    friendly message when a required column simply isn't present.
    """
    if path is None:
        path = DATA_DIR / "may_schedule.csv"
    df = pd.read_csv(path, encoding="latin-1")

    # 1) Strip UTF-8 BOM and stray whitespace from column headers so an Excel
    # export (which often saves as UTF-8 with BOM on Windows) doesn't
    # mysteriously fail the column lookup.
    df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]

    # 2) Resolve fuzzy column names to the canonical names downstream code
    # expects. Raises ScheduleColumnError on missing required columns.
    df = _resolve_schedule_columns(df)

    # 3) Coerce BUILD QTY to integer before filtering, so non-numeric junk
    # (section divider rows, empty strings) becomes NaN → dropped, and 0-qty
    # rows don't sneak through as phantom units.
    df["__qty_num"] = pd.to_numeric(df["BUILD QTY"], errors="coerce")
    df = df[df["FG SKU ID"].notna() & df["__qty_num"].notna() & (df["__qty_num"] > 0)].copy()
    df["BUILD QTY"] = df["__qty_num"].astype(int)
    df.drop(columns=["__qty_num"], inplace=True)

    # 4) Normalise text cols
    df["LOC"] = df["LOCATION"].astype(str).str.upper().str.strip()
    df["FG_RAW"] = df["FG SKU ID"].astype(str).str.strip()
    df["ACC"] = df["FG ACCRY SKU ID"].astype(str).str.strip().replace("nan", "")

    # 5) Normalise PRODUCTION MONTH to canonical Mon-YY where possible so the
    # downstream regex-based filter and the CARRYOVER flag work whether the
    # source uses Mon-YY, 5/8/2026, 2026-05-08, etc.
    df["PRODUCTION MONTH"] = _coerce_production_month(df["PRODUCTION MONTH"])

    # 6) Filter location first so month detection is scoped to the right rows
    if location:
        df = df[df["LOC"] == location.upper()]

    # 7) Detect carryover rows (YY-Mon format) vs current rows (Mon-YY format).
    # Use vectorized .str.match() with na=False — robust against PyArrow-backed
    # string columns (Python 3.14 + newer pandas default these), where Arrow
    # NA scalars can leak through .apply() and break re.match(). Casting to
    # object + fillna("") guarantees we feed real Python strings into the
    # vectorized regex.
    carryover_pattern_str = r"^\d{2}-[A-Za-z]{3}$"
    df["CARRYOVER"] = (
        df["PRODUCTION MONTH"]
        .astype("object")
        .fillna("")
        .astype(str)
        .str.match(carryover_pattern_str, na=False)
        .astype(bool)
    )

    # 8) Filter to detected months
    months = _detect_schedule_months(df, include_carryover)
    if months:
        df = df[df["PRODUCTION MONTH"].isin(months)]

    # 9) Parse PRODUCTION DAY → real datetime + derive WEEK_OF_MONTH.
    # The column is OPTIONAL; if it's not in the CSV (or every value is
    # blank) we attach all-NaT and downstream code treats the schedule as
    # whole-month-only. Weeks are Monday-anchored calendar weeks (see
    # core.constants.week_of_month) — week 1 is the calendar week containing
    # the 1st, so the first/last week may be partial.
    if "PRODUCTION DAY" in df.columns:
        parsed_day = pd.to_datetime(
            df["PRODUCTION DAY"].astype("object"),
            errors="coerce",
        )
    else:
        parsed_day = pd.Series([pd.NaT] * len(df), index=df.index)
    df["PRODUCTION DAY"] = parsed_day
    df["WEEK_OF_MONTH"] = week_of_month_series(parsed_day)

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
