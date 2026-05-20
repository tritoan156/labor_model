"""Labor & capacity calculations for the Henderson model.

Pure functions: take inputs, return DataFrames. No I/O, no UI.
"""
import math

import pandas as pd
import numpy as np

from .constants import (
    FINAL_LABOR, HS_FINAL_CREW, DEFAULT_NAMEPLATE_PREP, DEFAULT_BATT_RAW,
    ETO_LABOR_PER_UNIT, STATION_KEY_TO_DISPLAY, STATION_KEYS,
    BATTERY_CYCLE_MINUTES, get_battery_type,
)


# ---------------------------------------------------------------
# Per-unit labor
# ---------------------------------------------------------------

def classify_unit(fg_base: str) -> str:
    """Return unit class: HS (PM only), HT (Head Trailer), or STD."""
    s = str(fg_base).upper()
    if "HS" in s:
        return "HS"
    if "HT" in s:
        return "HT"
    return "STD"


def compute_unit_labor(fg_base: str, acc_sku: str | None,
                       machine_df: pd.DataFrame, acc_df: pd.DataFrame) -> dict:
    """Return per-station labor (person-mins) for one unit.

    Returns a dict with keys: Class, Bat, Warehouse, Wire, Battery, PMAcc,
    GenAcc, Trailer, AccKIT, Final, PDI, QC, Ship, ETO.

    Battery handling:
      - If Acc has BattSubRaw > 0 OR Nameplate Prep > 0, use its values
      - Else if Machine has Bat ≥ 1, use defaults (Nameplate Prep=10, BattRaw=320)
      - Else 0

    Final logic:
      - HS: 30 person-mins (single value, not 'crew × cycle')
      - HT: 60 person-mins (Mount only)
      - STD: 156 person-mins (Mount 60 + Marry 96 batched)

    ETO logic:
      - 960 person-mins for BOSS220/BOSS400 units (HS or otherwise)
      - 0 for everything else
    """
    if fg_base not in machine_df.index:
        return None
    m = machine_df.loc[fg_base]

    if acc_sku and acc_sku in acc_df.index:
        a = acc_df.loc[acc_sku]
    else:
        a = None

    cls = classify_unit(fg_base)

    # Battery applies only to BOSS units. PDS / SDG (diesel generators) may show
    # Bat=1 in the source machine catalog, but they have no batteries — so we
    # force their battery count and battery labor to 0.
    is_boss = str(fg_base).upper().startswith("BOSS")
    bat_in_catalog = int(m["Bat"]) if m["Bat"] else 0
    bat = bat_in_catalog if is_boss else 0

    # Defensive helper — read a labor column from the accessory row,
    # returning 0 if the column is missing (handles legacy CSVs that
    # were saved before a column rename, etc.)
    def _acc(col: str) -> float:
        if a is None:
            return 0.0
        try:
            v = a[col]
        except KeyError:
            return 0.0
        try:
            return float(v) if pd.notna(v) else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Battery total
    btr = _acc("BattSubRaw")
    nameplate = _acc("Nameplate Prep")
    if not is_boss:
        batt_total = 0
    elif a is not None and (btr > 0 or nameplate > 0):
        batt_total = btr * bat + nameplate
    elif bat > 0:
        batt_total = DEFAULT_BATT_RAW * bat + DEFAULT_NAMEPLATE_PREP
    else:
        batt_total = 0

    # ETO?
    fg_upper = str(fg_base).upper()
    is_eto = "BOSS220" in fg_upper or "BOSS400" in fg_upper
    eto = ETO_LABOR_PER_UNIT if is_eto else 0

    return {
        "Class": cls,
        "Bat": bat,
        "Warehouse": m["Warehouse"] + _acc("Warehouse"),
        "Wire": m["Wire"],
        "Battery": batt_total,
        "PMAcc": _acc("PMAcc"),
        "GenAcc": _acc("GenAcc"),
        "Trailer": m["Trailer"],
        "AccKIT": _acc("AccKIT"),
        "Final": FINAL_LABOR[cls],
        "PDI": m["PDI"],
        "QC": m["QC"],
        "Ship": m["Ship"],
        "ETO": eto,
    }


# ---------------------------------------------------------------
# Schedule aggregates
# ---------------------------------------------------------------

def expand_schedule(schedule_df: pd.DataFrame,
                     machine_df: pd.DataFrame,
                     acc_df: pd.DataFrame) -> pd.DataFrame:
    """Expand schedule rows into per-unit records with labor attached.

    Returns DataFrame with one row per individual unit (qty=1 each), columns
    include: unit_id, fg_base, fg_raw, decal, acc, customer, carryover,
    Class, Bat, Warehouse..., Final, PDI, QC, Ship, ETO, total_labor.
    """
    rows = []
    unit_id = 0
    for _, r in schedule_df.iterrows():
        qty = int(r["BUILD QTY"])
        labor = compute_unit_labor(r["FG_BASE"], r.get("ACC") or None, machine_df, acc_df)
        if labor is None:
            continue
        for _ in range(qty):
            unit_id += 1
            row = {
                "unit_id": unit_id,
                "fg_raw": r["FG_RAW"],
                "fg_base": r["FG_BASE"],
                "decal": r.get("DECAL", ""),
                "acc": r.get("ACC") or "",
                "customer": str(r.get("CUSTOMER NAME", "") or ""),
                "carryover": bool(r["CARRYOVER"]),
            }
            row.update(labor)
            row["total_labor"] = sum(labor.get(s, 0) for s in STATION_KEYS)
            row["batt_type"] = get_battery_type(r["FG_BASE"])
            rows.append(row)
    return pd.DataFrame(rows)


def station_demand_table(units_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-unit labor into station-level demand.

    Returns DataFrame indexed by station key with columns:
      demand (total person-mins), units_touching (count of units routed)
    """
    rows = []
    for st in STATION_KEYS:
        col = st
        demand = float(units_df[col].sum())
        units_touching = int((units_df[col] > 0).sum())
        rows.append({"station": st, "demand": demand, "units_touching": units_touching})
    return pd.DataFrame(rows).set_index("station")


def cycle_for_unit(station: str, total_labor: float, cls: str, crew: int) -> float:
    """Per-unit cycle time at a station, accounting for HS Final special case."""
    if total_labor == 0:
        return 0
    if station == "Final" and cls == "HS":
        return total_labor / HS_FINAL_CREW
    return total_labor / crew


def weighted_avg_cycle(units_df: pd.DataFrame, station: str, crew: int) -> float:
    """Mean cycle time across units that touch this station.

    Weighted by 1 unit each (units_df is already expanded).
    """
    mask = units_df[station] > 0
    if not mask.any():
        return 0
    cycles = []
    for _, r in units_df[mask].iterrows():
        cycles.append(cycle_for_unit(station, r[station], r["Class"], crew))
    return float(np.mean(cycles))


# ---------------------------------------------------------------
# Capacity vs Demand
# ---------------------------------------------------------------

def build_capacity_table(units_df: pd.DataFrame, crew_config: pd.DataFrame,
                          shift_minutes: int, working_days: int,
                          safety_factor: float,
                          efficiency_factor: float = 1.0) -> pd.DataFrame:
    """Build the headline capacity-vs-demand table.

    Args:
      units_df:    Output of expand_schedule()
      crew_config: DataFrame indexed by station_display with cols HC, Conc, Crew
      shift_minutes, working_days, safety_factor, efficiency_factor: scalar inputs.
        Effective capacity per HC = shift × days × efficiency × safety.

    Returns DataFrame indexed by station display name with columns:
      HC, Conc, Crew,
      labor_demand, labor_cap_safe, labor_util_safe, labor_status_safe,
      labor_cap_raw, labor_util_raw, labor_status_raw,
      avg_cycle, units_or_batt, need_per_day,
      thru_cap_safe, thru_util_safe, thru_status_safe,
      thru_cap_raw, thru_util_raw, thru_status_raw,
      required_hc, hc_gap,
      overall_status
    """
    demand_tbl = station_demand_table(units_df)

    rows = []
    for st_key in STATION_KEYS:
        st_disp = STATION_KEY_TO_DISPLAY[st_key]
        if st_disp not in crew_config.index:
            continue
        hc = int(crew_config.loc[st_disp, "HC"])
        conc = int(crew_config.loc[st_disp, "Conc"])
        crew = int(crew_config.loc[st_disp, "Crew"])

        labor_demand = float(demand_tbl.loc[st_key, "demand"])
        units_touching = int(demand_tbl.loc[st_key, "units_touching"])

        # Effective capacity layers:
        #   raw  = theoretical maximum (HC × shift × days)
        #   safe = raw × efficiency × safety  (planning capacity)
        labor_cap_raw = hc * shift_minutes * working_days
        labor_cap_safe = labor_cap_raw * efficiency_factor * safety_factor

        # Required HC to meet labor demand at the safe rate (rounded up).
        # Uses efficiency × safety so the number reflects realistic planning.
        denom = shift_minutes * working_days * efficiency_factor * safety_factor
        if denom > 0:
            required_hc = int(math.ceil(labor_demand / denom)) if labor_demand > 0 else 0
        else:
            required_hc = 0
        hc_gap = required_hc - hc  # +ve = short; -ve = surplus

        # For Battery: report in BATTERIES (not units), since multi-batt units
        # consume cell time differently.
        if st_key == "Battery":
            avg_cycle = BATTERY_CYCLE_MINUTES
            # Total batteries = sum of Bat counts for units that route through Battery
            batt_count = int((units_df.loc[units_df["Battery"] > 0, "Bat"]).sum())
            count_for_table = batt_count
        else:
            avg_cycle = weighted_avg_cycle(units_df, st_key, crew)
            count_for_table = units_touching

        need_per_day = count_for_table / working_days if working_days > 0 else 0
        if avg_cycle > 0:
            thru_cap_raw = conc * (shift_minutes / avg_cycle)
            thru_cap_safe = thru_cap_raw * efficiency_factor * safety_factor
        else:
            thru_cap_raw = thru_cap_safe = 0

        labor_util_safe = labor_demand / labor_cap_safe if labor_cap_safe > 0 else 0
        labor_util_raw = labor_demand / labor_cap_raw if labor_cap_raw > 0 else 0
        thru_util_safe = need_per_day / thru_cap_safe if thru_cap_safe > 0 else 0
        thru_util_raw = need_per_day / thru_cap_raw if thru_cap_raw > 0 else 0

        # Status flags — distinguish "station doesn't exist here" from real status
        station_missing = (hc == 0)
        has_demand = labor_demand > 0

        rows.append({
            "station_display": st_disp,
            "station_key": st_key,
            "HC": hc, "Conc": conc, "Crew": crew,
            "labor_demand": labor_demand,
            "labor_cap_safe": labor_cap_safe,
            "labor_util_safe": labor_util_safe,
            "labor_status_safe": _status_emoji(labor_util_safe, station_missing, has_demand),
            "labor_cap_raw": labor_cap_raw,
            "labor_util_raw": labor_util_raw,
            "labor_status_raw": _status_emoji(labor_util_raw, station_missing, has_demand),
            "avg_cycle": avg_cycle,
            "units_or_batt": count_for_table,
            "need_per_day": need_per_day,
            "thru_cap_safe": thru_cap_safe,
            "thru_util_safe": thru_util_safe,
            "thru_status_safe": _status_emoji(thru_util_safe, station_missing, has_demand),
            "thru_cap_raw": thru_cap_raw,
            "thru_util_raw": thru_util_raw,
            "thru_status_raw": _status_emoji(thru_util_raw, station_missing, has_demand),
            "required_hc": required_hc,
            "hc_gap": hc_gap,
            "station_missing": station_missing,
        })

    out = pd.DataFrame(rows).set_index("station_display")
    # Overall status — worst of labor & throughput
    out["overall_status"] = out.apply(_overall_status, axis=1)
    return out


def _status_emoji(util: float, station_missing: bool = False, has_demand: bool = False) -> str:
    """Return emoji for a utilization fraction.

    Special cases:
      - station_missing + no demand → "⚪" (N/A — station not at this facility)
      - station_missing + has demand → "🔴" (NO CAPACITY — units need a station that doesn't exist)
    """
    if station_missing:
        return "🔴" if has_demand else "⚪"
    if util > 1.0:
        return "🔴"
    if util > 0.9:
        return "🟠"
    if util > 0.75:
        return "🟡"
    return "🟢"


def _overall_status(row) -> str:
    """Worst of all four utilization statuses."""
    statuses = [
        row["labor_status_safe"],
        row["labor_status_raw"],
        row["thru_status_safe"],
        row["thru_status_raw"],
    ]
    if row.get("station_missing", False):
        return "🔴 NO STATION" if row.get("labor_demand", 0) > 0 else "⚪ N/A"
    if "🔴" in statuses:
        return "🔴 OVER"
    if "🟠" in statuses:
        return "🟠 NEAR-CAP"
    if "🟡" in statuses:
        return "🟡 TIGHT"
    return "🟢 OK"


# ---------------------------------------------------------------
# Battery throughput specifics
# ---------------------------------------------------------------

def battery_demand_by_sku(units_df: pd.DataFrame) -> pd.DataFrame:
    """Total batteries needed broken down by FG_BASE.

    Returns DataFrame with: fg_base, batt_type, units, batt_per_unit,
    total_batteries, pct_of_total.
    """
    g = units_df.groupby(["fg_base", "batt_type"]).agg(
        units=("unit_id", "count"),
        batt_per_unit=("Bat", "max"),
    ).reset_index()
    g["total_batteries"] = g["units"] * g["batt_per_unit"]
    grand = g["total_batteries"].sum()
    g["pct_of_total"] = g["total_batteries"] / grand if grand > 0 else 0
    g = g.sort_values(by="total_batteries", ascending=False).reset_index(drop=True)
    return g


def battery_demand_by_type(units_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate batteries by battery type."""
    bd = battery_demand_by_sku(units_df)
    g = bd.groupby("batt_type").agg(
        total_batteries=("total_batteries", "sum"),
        units_count=("units", "sum"),
    ).reset_index()
    grand = g["total_batteries"].sum()
    g["pct_of_total"] = g["total_batteries"] / grand if grand > 0 else 0
    g = g.sort_values(by="total_batteries", ascending=False).reset_index(drop=True)
    return g
