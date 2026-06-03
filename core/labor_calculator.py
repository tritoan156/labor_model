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


def classify_fg_category(fg_base: str) -> str:
    """Map an FG base SKU to its production LINE.

    The plant runs parallel production lines whose outputs ADD up:
      - "Compressor" — PDS* (portable diesel compressors)
      - "Generator"  — SDG* (generators, usually trailer-mounted, so they also
                       use the Trailer station)
      - "PM"         — BOSS head/skid-only units (Power Module; classify_unit=="HS")
      - "EBOSS"      — full BOSS units (head + trailer + PM)
      - "Other"      — anything else (none in the catalog today; kept as its own
                       line so nothing is ever silently dropped)

    Which STATIONS each line uses is NOT encoded here — it is read per unit from
    the catalog labor in ``buildable_by_line`` — so shared stations (e.g. Trailer
    used by both Generator and EBOSS) are handled automatically.
    """
    s = str(fg_base or "").upper().strip()
    if s.startswith("PDS"):
        return "Compressor"
    if s.startswith("SDG"):
        return "Generator"
    if s.startswith("BOSS"):
        return "PM" if classify_unit(s) == "HS" else "EBOSS"
    return "Other"


# ---------------------------------------------------------------
# Workforce-availability factors (new-hire ramp + absenteeism)
# ---------------------------------------------------------------

def _clamp01(x) -> float:
    """Clamp a value to [0, 1]; non-numeric / NaN → 0.0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def labor_availability_factor(new_hire_pct: float = 0.0,
                              new_hire_productivity: float = 1.0,
                              absenteeism: float = 0.0) -> float:
    """Fraction of nominal labor actually available, given two drags:

      * new-hire ramp — a share ``new_hire_pct`` of the crew works at only
        ``new_hire_productivity`` of a seasoned worker's pace, so the crew-wide
        multiplier is ``(1 - new_hire_pct * (1 - new_hire_productivity))``.
      * absenteeism — a share ``absenteeism`` of paid time is simply not worked,
        multiplying availability by ``(1 - absenteeism)``.

    All args are fractions in [0, 1]. Returns a multiplier in [0, 1] applied to
    LABOR capacity only — never to physical throughput / cells.
    """
    nh = _clamp01(new_hire_pct)
    prod = _clamp01(new_hire_productivity)
    absent = _clamp01(absenteeism)
    return (1.0 - nh * (1.0 - prod)) * (1.0 - absent)


def adjusted_efficiency(base_efficiency: float,
                        new_hire_pct: float = 0.0,
                        new_hire_productivity: float = 1.0) -> float:
    """Base efficiency reduced by new-hire ramp only (for DISPLAY).

    Mirrors the legacy planning spreadsheet's "Adjusted Efficiency" row:
    ``base * (1 - new_hire_pct * (1 - new_hire_productivity))``. Absenteeism is
    reported and applied as its own separate term, so it is NOT folded in here.
    """
    nh = _clamp01(new_hire_pct)
    prod = _clamp01(new_hire_productivity)
    return float(base_efficiency) * (1.0 - nh * (1.0 - prod))


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
                          efficiency_factor: float = 1.0,
                          new_hire_pct: float = 0.0,
                          new_hire_productivity: float = 1.0,
                          absenteeism: float = 0.0,
                          overtime: float = 0.0) -> pd.DataFrame:
    """Build the headline capacity-vs-demand table.

    Args:
      units_df:    Output of expand_schedule()
      crew_config: DataFrame indexed by station_display with cols HC, Conc, Crew
      shift_minutes, working_days, safety_factor, efficiency_factor: scalar inputs.
        Effective capacity per HC = shift × days × efficiency × safety.
      new_hire_pct, new_hire_productivity, absenteeism: workforce drags. These
        multiply a labor-availability factor into LABOR capacity and Required-HC
        ONLY (never into ``thru_cap_safe``), so physical throughput / Space-limits
        are unaffected by staffing availability. Defaults make them a no-op.
      overtime: extra working time as a fraction (0.20 = +20%). Extends the
        effective shift, so it raises BOTH labor capacity AND throughput
        (cells run longer) — the one factor that lifts Space-limits. No-op at 0.

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

    # Labor-availability drag (new-hire ramp + absenteeism). Applied to LABOR
    # capacity and Required-HC only — NEVER to physical throughput (cells), so
    # the Space-limits view stays judged purely on equipment, not staffing.
    labor_factor = labor_availability_factor(
        new_hire_pct, new_hire_productivity, absenteeism)

    # Overtime extends working time, so it scales the effective shift used for
    # BOTH labor capacity and throughput (cells run longer → more units/day).
    effective_shift = shift_minutes * (1.0 + max(0.0, float(overtime or 0.0)))

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
        labor_cap_raw = hc * effective_shift * working_days
        labor_cap_safe = labor_cap_raw * efficiency_factor * safety_factor * labor_factor

        # Required HC to meet labor demand at the safe rate (rounded up).
        # Uses efficiency × safety × labor-availability so the number reflects
        # realistic planning (new-hire ramp + absenteeism raise the HC needed).
        denom = effective_shift * working_days * efficiency_factor * safety_factor * labor_factor
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
            thru_cap_raw = conc * (effective_shift / avg_cycle)
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


def buildable_by_line(units_df: pd.DataFrame, capacity: pd.DataFrame) -> dict:
    """Line-aware buildable-unit cap.

    The plant runs parallel production lines (see :func:`classify_fg_category`)
    whose outputs ADD up. The current SKU mix can be scaled WITHIN each line
    independently up to that line's own bottleneck; one line is NOT throttled by
    another line's constraint (e.g. PDS/compressor is not limited by the Battery
    station, which only EBOSS/PM use).

    For each line::

        buildable(line) = floor( planned_units(line) / max_util(line) )
        max_util(line)  = max, over the stations that line uses, of
                          max(labor_util_safe, thru_util_safe)

    where the station utilizations come from ``capacity`` (built by
    :func:`build_capacity_table`) and are computed from the COMBINED demand of
    every line that uses the station. That makes this a *constrained sum*: the
    per-line caps provably never sum to more than any station's capacity, because
    a station shared by several lines caps each of them by ``1/util`` and
    ``Σ demand/util = capacity``. The planned line mix is preserved.

    A station a line needs but with HC=0 makes that line infeasible (buildable 0),
    mirroring the missing-station handling elsewhere.

    Returns ``{"lines": [ {line, planned, buildable, binding_station,
    binding_kind, max_util, infeasible} ... ], "total": int}`` (lines sorted by
    name). Empty inputs return ``{"lines": [], "total": 0}``.
    """
    if (units_df is None or units_df.empty
            or capacity is None or capacity.empty
            or "fg_base" not in units_df.columns):
        return {"lines": [], "total": 0}

    def _station_util(disp: str):
        """(util, kind) for a station display name; inf if needed-but-unstaffed."""
        if disp not in capacity.index:
            return 0.0, "labor"
        row = capacity.loc[disp]
        if int(row.get("HC", 0) or 0) <= 0:
            return float("inf"), "labor"
        lu = float(row.get("labor_util_safe", 0) or 0)
        tu = float(row.get("thru_util_safe", 0) or 0)
        return (lu, "labor") if lu >= tu else (tu, "throughput")

    line_of = units_df["fg_base"].map(classify_fg_category)
    rows = []
    total = 0
    for line in sorted(set(line_of)):
        sub = units_df[line_of == line]
        planned = int(len(sub))
        if planned == 0:
            continue
        max_util = 0.0
        binding = ""
        binding_kind = "labor"
        infeasible = False
        for st_key in STATION_KEYS:
            if st_key not in sub.columns or float(sub[st_key].sum()) <= 0:
                continue  # this line doesn't use this station
            disp = STATION_KEY_TO_DISPLAY[st_key]
            util, kind = _station_util(disp)
            if util == float("inf"):
                infeasible, max_util, binding, binding_kind = True, util, disp, kind
                break
            if util > max_util:
                max_util, binding, binding_kind = util, disp, kind
        if infeasible:
            buildable = 0
        elif max_util <= 0:
            buildable = planned
        else:
            buildable = int(planned // max_util)
        total += buildable
        rows.append({
            "line": line,
            "planned": planned,
            "buildable": buildable,
            "binding_station": binding or "—",
            "binding_kind": binding_kind,
            "max_util": (None if max_util in (0.0, float("inf")) else round(max_util, 4)),
            "infeasible": infeasible,
        })
    return {"lines": rows, "total": int(total)}


# ---------------------------------------------------------------
# Executive summary (monthly labor roll-up, mirrors legacy spreadsheet)
# ---------------------------------------------------------------

def executive_summary(*, total_earned_minutes: float, total_units: int,
                      available_hc: float, shift_minutes: int, working_days: int,
                      base_efficiency: float, new_hire_pct: float = 0.0,
                      new_hire_productivity: float = 1.0, absenteeism: float = 0.0,
                      safety: float = 1.0, overtime: float = 0.0,
                      support_hc: float = 0.0,
                      station_required_hc: float | None = None,
                      station_buildable: int | None = None) -> dict:
    """Monthly executive labor summary (mirrors the legacy planning spreadsheet).

    Aggregate (poolable-labor) view: treats total earned hours as fully shareable
    across stations, exactly like the legacy sheet. The station-aware figures
    (which respect per-station bottlenecks) are passed in by the caller via
    ``station_required_hc`` / ``station_buildable`` and surfaced alongside.

    Formulas (all hours are person-hours)::

        effective_shift            = shift * (1 + overtime)
        total_earned_hours         = total_earned_minutes / 60
        adjusted_efficiency        = base * (1 - NH*(1-NHprod))            [display]
        effective_hours_per_person = (effective_shift*days/60) * adj_eff * (1-absent) * safety
        headcount_needed_aggregate = earned / effective_hours_per_person
        net_available_hours        = available_hc * effective_shift*days/60   [gross]
        available_earned_hours     = available_hc * effective_hours_per_person
        missed_units_aggregate     = units * max(0, 1 - avail_earned/earned)
        verdict                    = "Good" if available_hc >= hc_needed else "No Good"

    ``available_hc`` is PRODUCTION headcount (drives the verdict / coverage).
    ``support_hc`` (Lead/Other) is non-production: it adds only to
    ``total_headcount`` and ``total_available_hours`` (cost / staffing view) and
    never improves the verdict. ``overtime`` (e.g. 0.20 = +20%) extends the
    effective shift, raising available hours. ``safety`` is the app's multiplier
    (1.00 = no buffer). Returns a flat dict of every row the table renders.
    """
    earned_hr = float(total_earned_minutes or 0.0) / 60.0
    units = int(total_units or 0)
    avail_hc = float(available_hc or 0.0)
    support = float(support_hc or 0.0)
    # Overtime extends working time → more hours per person (lifts capacity).
    effective_shift = float(shift_minutes or 0) * (1.0 + max(0.0, float(overtime or 0.0)))
    hours_per_person = (effective_shift * float(working_days or 0)) / 60.0

    adj_eff = adjusted_efficiency(base_efficiency, new_hire_pct, new_hire_productivity)
    absent = _clamp01(absenteeism)
    safety_mult = _clamp01(safety)
    eff_hours_per_person = hours_per_person * adj_eff * (1.0 - absent) * safety_mult

    # Man-hours per unit. ``mh_per_unit`` is the STANDARD work content (earned
    # hours ÷ units). ``mh_per_unit_loaded`` is how many PAID man-hours each unit
    # actually consumes at this scenario's efficiency = standard ÷ productive
    # fraction (base eff × new-hire ramp × (1-absent)). Overtime and safety move
    # capacity / buffer, not per-unit labor cost, so they're excluded here.
    mh_per_unit = (earned_hr / units) if units > 0 else 0.0
    _prod_frac = adj_eff * (1.0 - absent)
    mh_per_unit_loaded = (mh_per_unit / _prod_frac) if _prod_frac > 0 else 0.0

    hc_needed_agg = (earned_hr / eff_hours_per_person) if eff_hours_per_person > 0 else 0.0
    net_available_hours = avail_hc * hours_per_person
    available_earned_hours = avail_hc * eff_hours_per_person

    # Support roles (Lead/Other) add to total headcount and total available
    # labor-hours only — never to the production earned-hours / verdict above.
    total_headcount = avail_hc + support
    total_available_hours = total_headcount * hours_per_person
    # Fully-burdened paid hours per unit: ALL staffed hours (production +
    # support, gross incl. OT) spread over the planned units. A supply/cost view
    # — distinct from the demand-side mh/unit above. Rises with support staff.
    total_available_hours_per_unit = (total_available_hours / units) if units > 0 else 0.0

    if earned_hr > 0:
        coverage = available_earned_hours / earned_hr
        missed_agg = units * max(0.0, 1.0 - coverage)
    else:
        coverage = 1.0
        missed_agg = 0.0

    verdict = "Good" if avail_hc >= hc_needed_agg else "No Good"

    # Station-aware figures (optional — computed by the caller from the live
    # capacity table, which already includes the same labor factor).
    hc_needed_station = (float(station_required_hc)
                         if station_required_hc is not None else None)
    missed_station = (max(0, units - int(station_buildable))
                      if station_buildable is not None else None)

    return {
        "total_earned_hours": earned_hr,
        "total_units": units,
        "units_per_day": (units / working_days) if working_days else 0.0,
        "mh_per_unit": mh_per_unit,
        "mh_per_unit_loaded": mh_per_unit_loaded,
        "total_available_hours_per_unit": total_available_hours_per_unit,
        "available_headcount": avail_hc,
        "support_headcount": support,
        "total_headcount": total_headcount,
        "base_efficiency": float(base_efficiency),
        "new_hire_pct": _clamp01(new_hire_pct),
        "new_hire_productivity": _clamp01(new_hire_productivity),
        "absenteeism": absent,
        "overtime": max(0.0, float(overtime or 0.0)),
        "safety": safety_mult,
        "adjusted_efficiency": adj_eff,
        "effective_hours_per_person": eff_hours_per_person,
        "net_available_hours": net_available_hours,
        "total_available_hours": total_available_hours,
        "available_earned_hours": available_earned_hours,
        "headcount_needed_aggregate": hc_needed_agg,
        "headcount_needed_station": hc_needed_station,
        "missed_units_aggregate": missed_agg,
        "missed_units_station": missed_station,
        "coverage_pct": coverage * 100.0,
        "verdict": verdict,
    }


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
