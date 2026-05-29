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
    UTIL_THRESHOLD_OVER, UTIL_THRESHOLD_NEAR, UTIL_THRESHOLD_TIGHT,
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
      - Read directly from the machine catalog's ``FN_Assy`` column (the
        person-minutes the planner enters per FG SKU). Used literally,
        including 0 — a blank/0 FN_Assy means no Final-assembly labor for
        that SKU.

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
    bat_in_catalog = int(m["Bat"]) if (pd.notna(m["Bat"]) and m["Bat"]) else 0
    bat = bat_in_catalog if is_boss else 0

    # Defensive helper — read a machine labor column, returning 0 for a
    # missing/NaN cell. Mirrors ``unit_labor_split._m`` so the per-station
    # labor and the machine/acc split treat NaN identically (keeps their
    # sums equal even if a catalog cell ever slips through un-sanitized).
    def _m(col: str) -> float:
        try:
            v = m[col]
        except (KeyError, TypeError):
            return 0.0
        try:
            return float(v) if pd.notna(v) else 0.0
        except (TypeError, ValueError):
            return 0.0

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

    # ETO — read from the machine catalog's ETO column when present (the
    # planner enters it per FG SKU). Fall back to the legacy rule (960 p-min
    # for BOSS220/BOSS400) only if the column is missing, e.g. a stale cache.
    fg_upper = str(fg_base).upper()
    if "ETO" in m.index:
        try:
            eto = float(m["ETO"]) if pd.notna(m["ETO"]) else 0.0
        except (TypeError, ValueError):
            eto = 0.0
    else:
        eto = ETO_LABOR_PER_UNIT if ("BOSS220" in fg_upper or "BOSS400" in fg_upper) else 0

    # Final Assembly is taken straight from the machine catalog's FN_Assy
    # column (person-minutes the planner enters per FG SKU), used literally.
    try:
        final_labor = float(m["FN_Assy"]) if pd.notna(m["FN_Assy"]) else 0.0
    except (KeyError, TypeError, ValueError):
        final_labor = 0.0

    # FN_Assy / AccKIT / PDI can each be routed to a different station per SKU
    # (e.g. PDS compressors are actually built by the Compressor team, so all
    # three of their labor lines land at ComAcc, not Final / AccKIT / PDI).
    # The respective "<X> Station" catalog columns carry the target station
    # key; the loader has already validated against STATION_KEYS, so unknown
    # values can never land here — but we add a final defensive set anyway.
    _VALID = {"Final", "ComAcc", "GenAcc", "PMAcc", "Warehouse", "Wire",
              "Battery", "Trailer", "AccKIT", "PDI", "QC", "Ship", "ETO"}

    def _station(col, default):
        try:
            v = m[col]
        except (KeyError, TypeError):
            return default
        if not pd.notna(v):
            return default
        s = str(v).strip()
        return s if s in _VALID else default

    final_station  = _station("Final Station",  "Final")
    acckit_station = _station("AccKIT Station", "AccKIT")
    pdi_station    = _station("PDI Station",    "PDI")

    # Source labor lines — populated by their catalog values, then *routed*
    # into the result dict at the chosen station below.
    acckit_labor = _acc("AccKIT")
    try:
        pdi_labor = float(m["PDI"]) if pd.notna(m["PDI"]) else 0.0
    except (KeyError, TypeError, ValueError):
        pdi_labor = 0.0

    result = {
        "Class": cls,
        "Bat": bat,
        "Warehouse": _m("Warehouse") + _acc("Warehouse"),
        "Wire": _m("Wire"),
        "Battery": batt_total,
        "PMAcc": _acc("PMAcc"),
        "GenAcc": _acc("GenAcc"),
        "ComAcc": _acc("ComAcc"),
        "Trailer": _m("Trailer"),
        "AccKIT": 0.0,
        "Final": 0.0,
        "PDI": 0.0,
        "QC": _m("QC"),
        "Ship": _m("Ship"),
        "ETO": eto,
    }
    # Route each labor line to its target station. When the target is the
    # original station the behavior matches the previous (unrouted) version.
    result[final_station]  = float(result.get(final_station, 0) or 0) + final_labor
    result[acckit_station] = float(result.get(acckit_station, 0) or 0) + acckit_labor
    result[pdi_station]    = float(result.get(pdi_station, 0) or 0) + pdi_labor
    return result


def unit_labor_split(fg_base: str, acc_sku: str | None,
                     machine_df: pd.DataFrame, acc_df: pd.DataFrame) -> dict | None:
    """Split one unit's per-unit labor by source: the machine (FG) catalog vs
    the accessory catalog. Returns ``{"machine": float, "acc": float}`` whose
    sum equals the total from :func:`compute_unit_labor`. Returns None if the
    FG isn't in the machine catalog.

    Attribution mirrors ``compute_unit_labor``:
      • Machine side — Warehouse(FG), Wire, Trailer, PDI, QC, Ship, Final
        assembly, ETO, and battery labor when it comes from the machine
        defaults (accessory supplied none).
      • Accessory side — Warehouse(acc), PM/Gen/Com accessories, Accessory KIT,
        and battery labor when the accessory row supplies it
        (BattSubRaw / Nameplate Prep).
    """
    if fg_base not in machine_df.index:
        return None
    m = machine_df.loc[fg_base]
    a = acc_df.loc[acc_sku] if (acc_sku and acc_sku in acc_df.index) else None

    cls = classify_unit(fg_base)
    is_boss = str(fg_base).upper().startswith("BOSS")
    bat_in_catalog = int(m["Bat"]) if (pd.notna(m["Bat"]) and m["Bat"]) else 0
    bat = bat_in_catalog if is_boss else 0

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

    def _m(col: str) -> float:
        try:
            return float(m[col]) if pd.notna(m[col]) else 0.0
        except (TypeError, ValueError, KeyError):
            return 0.0

    btr = _acc("BattSubRaw")
    nameplate = _acc("Nameplate Prep")
    if not is_boss:
        batt_total, batt_from_acc = 0.0, False
    elif a is not None and (btr > 0 or nameplate > 0):
        batt_total, batt_from_acc = btr * bat + nameplate, True
    elif bat > 0:
        batt_total, batt_from_acc = DEFAULT_BATT_RAW * bat + DEFAULT_NAMEPLATE_PREP, False
    else:
        batt_total, batt_from_acc = 0.0, False

    # ETO from the catalog (fallback to the legacy BOSS220/400 rule if the
    # column is missing), mirroring compute_unit_labor.
    fg_upper = str(fg_base).upper()
    if "ETO" in m.index:
        eto = _m("ETO")
    else:
        eto = ETO_LABOR_PER_UNIT if ("BOSS220" in fg_upper or "BOSS400" in fg_upper) else 0

    machine = (
        _m("Warehouse") + _m("Wire") + _m("Trailer")
        + _m("PDI") + _m("QC") + _m("Ship")
        + _m("FN_Assy") + eto
        + (0.0 if batt_from_acc else batt_total)
    )
    acc = (
        _acc("Warehouse") + _acc("PMAcc") + _acc("GenAcc") + _acc("ComAcc")
        + _acc("AccKIT")
        + (batt_total if batt_from_acc else 0.0)
    )
    return {"machine": machine, "acc": acc}


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

    Two helper lists are attached to the returned DataFrame as attributes so
    callers can surface data-quality warnings without breaking the existing
    callsites:

      ``.attrs['skipped_fg']``       — FG SKUs in the schedule that aren't in
                                       ``machine_clean.csv``. Their rows are
                                       silently dropped because we have no
                                       labor data for them.
      ``.attrs['unknown_acc']``      — Accessory SKUs referenced by a kept
                                       schedule row but not present in
                                       ``acc_clean.csv``. The unit is still
                                       built (with zero accessory labor),
                                       which may not be the planner's intent.
    """
    rows = []
    unit_id = 0
    skipped_fg: set[str] = set()
    unknown_acc: set[str] = set()
    acc_index = set(acc_df.index) if acc_df is not None else set()
    for _, r in schedule_df.iterrows():
        qty = int(r["BUILD QTY"])
        fg_base = r.get("FG_BASE")
        acc_sku = (r.get("ACC") or "") or None
        labor = compute_unit_labor(fg_base, acc_sku, machine_df, acc_df)
        if labor is None:
            if fg_base:
                skipped_fg.add(str(fg_base))
            continue
        # The unit kept — note any accessory we couldn't resolve so the
        # planner knows the labor at PMAcc/GenAcc/ComAcc/AccKIT/Battery is
        # zero by default, not measured.
        if acc_sku and acc_sku not in acc_index:
            unknown_acc.add(str(acc_sku))
        for _ in range(qty):
            unit_id += 1
            row = {
                "unit_id": unit_id,
                "fg_raw": r["FG_RAW"],
                "fg_base": fg_base,
                "decal": r.get("DECAL", ""),
                "acc": r.get("ACC") or "",
                "customer": str(r.get("CUSTOMER NAME", "") or ""),
                "carryover": bool(r["CARRYOVER"]),
            }
            row.update(labor)
            row["total_labor"] = sum(labor.get(s, 0) for s in STATION_KEYS)
            row["batt_type"] = get_battery_type(fg_base)
            rows.append(row)
    out = pd.DataFrame(rows)
    out.attrs["skipped_fg"] = sorted(skipped_fg)
    out.attrs["unknown_acc"] = sorted(unknown_acc)
    return out


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
    # Defensive: a crew of 0 is an invalid config (the UI enforces ≥1, but the
    # facility_crew.json is hand-editable / written via the GitHub API). Avoid a
    # ZeroDivisionError that would crash the whole capacity table.
    if crew <= 0:
        return 0.0
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
        # A staffed station that has real throughput demand but zero throughput
        # capacity can physically build nothing — yet the divide-by-zero guard
        # above forces thru_util to 0, which would otherwise render a misleading
        # 🟢. Flag it 🔴, mirroring the "missing station + demand" rule. This
        # happens when Conc=0 (no concurrent bays — user-editable down to 0 in
        # the crew panel) or avg_cycle<=0 (e.g. an invalid Crew=0 config).
        thru_blocked = ((conc == 0 or avg_cycle <= 0) and need_per_day > 0)

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
            "thru_status_safe": "🔴" if thru_blocked else _status_emoji(thru_util_safe, station_missing, has_demand),
            "thru_cap_raw": thru_cap_raw,
            "thru_util_raw": thru_util_raw,
            "thru_status_raw": "🔴" if thru_blocked else _status_emoji(thru_util_raw, station_missing, has_demand),
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
    if util > UTIL_THRESHOLD_OVER:
        return "🔴"
    if util > UTIL_THRESHOLD_NEAR:
        return "🟠"
    if util > UTIL_THRESHOLD_TIGHT:
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
