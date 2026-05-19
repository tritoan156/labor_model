"""Data quality checks for the Machine and Accessory catalogs.

Runs a series of rules over the loaded DataFrames and returns a list of
issues with severity + recommendation. Surfaced in the "Data Validation"
tab so problems remain visible after any CSV update.
"""
from __future__ import annotations

import re
from typing import Literal

import pandas as pd

Severity = Literal["error", "warning", "info"]


def _add(issues: list, severity: Severity, category: str, sku: str, message: str) -> None:
    issues.append({
        "Severity": severity,
        "Category": category,
        "SKU": sku,
        "Issue": message,
    })


def validate_machine_catalog(machine_df: pd.DataFrame) -> list[dict]:
    """Run validation rules over the machine DataFrame."""
    issues: list[dict] = []

    for sku, row in machine_df.iterrows():
        desc = str(row.get("Description", ""))

        # 1) Estimation placeholders ("XXX" in SKU)
        if "XXX" in str(sku).upper():
            _add(issues, "warning", "Estimation placeholder", str(sku),
                 "SKU contains 'XXX' — labor values are an estimate, not measured.")

        # 2) Trailing/leading whitespace in SKU
        if str(sku) != str(sku).strip():
            _add(issues, "error", "Whitespace in SKU", repr(str(sku)),
                 "SKU has leading or trailing whitespace — will cause lookup failures.")

        # 3) Accessory-like SKU in machine catalog
        if re.search(r"-A\d", str(sku)):
            _add(issues, "error", "Misplaced row", str(sku),
                 "SKU looks like an accessory (-A### pattern) but is in the machine catalog.")

        # 4) HS unit with non-zero Trailer labor
        if "HS" in str(sku).upper() and row.get("Trailer", 0) > 0:
            _add(issues, "warning", "HS with Trailer labor", str(sku),
                 f"HS (head-skid) units typically have Trailer=0; "
                 f"this row has Trailer={row['Trailer']}.")

        # 5) Missing description
        if not desc.strip():
            _add(issues, "warning", "Missing description", str(sku),
                 "Description is blank.")

        # 6) Flagged rows (Flag column non-empty)
        flag = row.get("Flag", "")
        if isinstance(flag, str) and flag.strip() and flag.lower() != "nan":
            _add(issues, "info", "Flagged for verification", str(sku),
                 f"Operator note: {flag.strip()[:120]}")

        # 7) LR / ST abbreviations in description (per HANDOFF.md)
        if re.search(r"\bLR\b", desc):
            _add(issues, "info", "LR abbreviation", str(sku),
                 "Description contains 'LR' — confirm meaning with operations.")
        if re.search(r"\bST\b", desc):
            _add(issues, "warning", "ST abbreviation", str(sku),
                 "Description contains 'ST' — flagged in HANDOFF as likely typo.")

        # 8) Non-BOSS units with Bat > 0 (PDS/SDG should have Bat=0)
        if row.get("Bat", 0) and row.get("Bat", 0) > 0 \
                and not str(sku).upper().startswith("BOSS"):
            _add(issues, "info", "Non-BOSS with Bat>0", str(sku),
                 f"Bat={row['Bat']} but SKU is not BOSS — battery labor is "
                 "ignored. Consider setting Bat=0 in the source CSV.")

    return issues


def validate_accessory_catalog(acc_df: pd.DataFrame, machine_df: pd.DataFrame) -> list[dict]:
    """Run validation rules over the accessory DataFrame."""
    issues: list[dict] = []

    for sku, row in acc_df.iterrows():
        desc = str(row.get("Description", ""))

        # 1) Estimation placeholders ("AXXX" in SKU)
        if "AXXX" in str(sku).upper() or "XXX" in str(sku).upper():
            _add(issues, "warning", "Estimation placeholder", str(sku),
                 "SKU contains 'XXX' — labor values are an estimate, not measured.")

        # 2) Trailing/leading whitespace
        if str(sku) != str(sku).strip():
            _add(issues, "error", "Whitespace in SKU", repr(str(sku)),
                 "SKU has leading or trailing whitespace — will cause lookup failures.")

        # 3) All labor columns are zero (suspicious)
        labor_cols = ["Warehouse", "AccKIT", "BattPrep", "BattSubRaw",
                      "PMAcc", "GenAcc", "Compressor"]
        labor_total = sum(float(row.get(c, 0) or 0) for c in labor_cols)
        if labor_total == 0:
            _add(issues, "warning", "Zero labor", str(sku),
                 "All labor columns are zero — accessory has no work attached.")

        # 4) LR / ST abbreviations
        if re.search(r"\bLR\b", desc):
            _add(issues, "info", "LR abbreviation", str(sku),
                 "Description contains 'LR' — confirm meaning with operations.")
        if re.search(r"\bST\b", desc):
            _add(issues, "warning", "ST abbreviation", str(sku),
                 "Description contains 'ST' — flagged in HANDOFF as likely typo.")

        # 5) Missing description
        if not desc.strip():
            _add(issues, "warning", "Missing description", str(sku),
                 "Description is blank.")

        # 6) Flagged rows
        flag = row.get("Flag", "")
        if isinstance(flag, str) and flag.strip() and flag.lower() != "nan":
            _add(issues, "info", "Flagged for verification", str(sku),
                 f"Operator note: {flag.strip()[:120]}")

    # 7) Machine SKUs that need batteries but no matching accessory carries battery labor
    #    (informational — useful for spotting catalog gaps)
    bat_machines = machine_df[machine_df["Bat"] > 0]
    families = set()
    for sku in bat_machines.index:
        # Family = SKU prefix up to first - or full SKU
        family = re.split(r"[-A]\d", str(sku))[0]
        if family:
            families.add(family)

    # Check if any accessory in each family has BattSubRaw > 0
    for family in sorted(families):
        family_accs = acc_df[acc_df.index.str.startswith(family + "-")]
        if family_accs.empty:
            _add(issues, "info", "No accessories", family,
                 f"Family '{family}' has machine SKUs needing batteries but "
                 f"no accessory rows exist — will use DEFAULT_BATT_RAW fallback.")
        elif not (family_accs["BattSubRaw"] > 0).any():
            _add(issues, "warning", "No battery labor in family", family,
                 f"Family '{family}' has accessories, but none carries "
                 f"BattSubRaw labor — units will use DEFAULT_BATT_RAW fallback.")

    return issues


def validate_all(machine_df: pd.DataFrame, acc_df: pd.DataFrame) -> pd.DataFrame:
    """Run all validations and return a single DataFrame for display."""
    issues = []
    issues.extend(validate_machine_catalog(machine_df))
    issues.extend(validate_accessory_catalog(acc_df, machine_df))
    df = pd.DataFrame(issues)
    if df.empty:
        return df
    # Sort: errors first, then warnings, then info
    sev_order = {"error": 0, "warning": 1, "info": 2}
    df["__sort"] = df["Severity"].map(sev_order)
    df = df.sort_values(by=["__sort", "Category", "SKU"]).drop(columns="__sort").reset_index(drop=True)
    # Prepend an emoji to severity for visual scanning
    sev_emoji = {"error": "🔴 Error", "warning": "🟡 Warning", "info": "ℹ️ Info"}
    df["Severity"] = df["Severity"].map(sev_emoji)
    return df
