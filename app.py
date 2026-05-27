"""Labor Capacity Tool (Henderson / Spartanburg / Cypress) — Streamlit web app.

Run with:   streamlit run app.py

The app reads CSV inputs from data/, lets the user adjust crew/safety/days
in the sidebar, and shows 8 dashboards as tabs.
"""
from __future__ import annotations

import calendar
import io
import re
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.data_loader import (
    load_machine_labor, load_acc_labor, load_schedule, build_manual_schedule,
    load_item_master, load_item_packages,
    resolve_item_time, unique_abbrs,
    ScheduleColumnError,
)
from core.labor_calculator import (
    expand_schedule, build_capacity_table,
    battery_demand_by_sku, battery_demand_by_type,
    compute_unit_labor, unit_labor_split,
)
from core.constants import (
    LOCATIONS, STATION_DEFAULTS, DEFAULT_SHIFT_MINUTES, DEFAULT_WORKING_DAYS,
    DEFAULT_SAFETY_FACTOR, DEFAULT_EFFICIENCY_FACTOR,
    STATION_KEYS, CUSTOMER_SUFFIXES,
    now_local, today_local_str, week_of_month, week_of_month_series,
)
from core.facility_storage import (
    load_facility_crew_df, save_facility_crew_to_github,
)
from core.catalog_storage import (
    save_catalog_to_github,
    fetch_catalog_csv_from_github,
    latest_catalog_sha,
    GitHubConflict,
)
from core.scenario_storage import (
    load_all_scenarios,
    list_scenarios,
    get_scenario,
    save_scenario_to_github,
    delete_scenario_on_github,
)
from core.uploaded_schedule_storage import (
    load_all_uploaded_schedules,
    list_uploaded_schedules,
    get_uploaded_schedule,
    save_uploaded_schedule_to_github,
    delete_uploaded_schedule_on_github,
    SCHEDULES_PATH as _UPLOADED_SCHEDULES_PATH,
)
from core.process_flow_storage import (
    load_process_flow,
    save_process_flow_to_github,
    reset_process_flow_to_default,
    DEFAULT_EDGES as PROCESS_FLOW_DEFAULTS,
    VALID_CLASSES as PROCESS_FLOW_VALID_CLASSES,
)
from core.data_validator import validate_all
from core.usage_tracker import (
    track_event,
    flush_to_github as _flush_usage_to_github,
    load_usage_log,
    refresh_from_github as _refresh_usage_from_github,
    get_buffer_snapshot as _usage_buffer_snapshot,
    USAGE_LOG_PATH as _USAGE_LOG_PATH,
)

DATA_DIR = Path(__file__).resolve().parent / "data"


# =============================================================
# Page config & styling
# =============================================================
st.set_page_config(
    page_title="Labor Capacity Tool",
    page_icon="🏭",
    layout="wide",
)

# A few small style tweaks for a cleaner, more dashboard-like look.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
      [data-testid="stMetricValue"] { font-size: 1.55rem; }
      [data-testid="stMetricLabel"] { font-size: 0.85rem; opacity: 0.9; }
      h1, h2, h3 { letter-spacing: -0.01em; }
      /* Tighter tab labels */
      .stTabs [data-baseweb="tab-list"] { gap: 6px; }
      .stTabs [data-baseweb="tab"] { padding-left: 14px; padding-right: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# =============================================================
# Data loading (cached, with file-mtime invalidation)
# =============================================================
def _csv_mtime(filename: str) -> float:
    """Return the mtime of a CSV under data/, or 0 if missing.

    Including this value as a cache argument makes Streamlit invalidate the
    cached DataFrame whenever the CSV file is updated (e.g. via git push).
    """
    p = DATA_DIR / filename
    return p.stat().st_mtime if p.exists() else 0.0


# Cache version — bump this when the loader's OUTPUT SCHEMA changes (column
# renames, new columns, etc.) so the cache invalidates even if the underlying
# CSV file's mtime hasn't changed.
_LOADER_SCHEMA_VERSION = 5


@st.cache_data(show_spinner=False)
def _load_machine_df(_mtime: float, _schema_ver: int = _LOADER_SCHEMA_VERSION):
    return load_machine_labor()


@st.cache_data(show_spinner=False)
def _load_acc_df(_mtime: float, _schema_ver: int = _LOADER_SCHEMA_VERSION):
    """Load acc_clean.csv. `_schema_ver` forces a cache refresh after schema
    changes even when the file mtime is unchanged."""
    return load_acc_labor()


@st.cache_data(show_spinner=False)
def _load_item_master_df(_mtime: float):
    return load_item_master()


@st.cache_data(show_spinner=False)
def _load_item_packages_df(_mtime: float):
    return load_item_packages()


@st.cache_data(show_spinner=False)
def _load_scenarios_dict(_mtime: float):
    """Load data/scenarios.json. Cached per file mtime."""
    return load_all_scenarios()


@st.cache_data(show_spinner=False)
def _load_uploaded_schedules_dict(_mtime: float):
    """Load data/uploaded_schedules.json. Cached per file mtime."""
    return load_all_uploaded_schedules()


@st.cache_data(show_spinner=False)
def _load_process_flow_edges(_mtime: float):
    """Load data/process_flow.json. Cached per file mtime."""
    return load_process_flow()


@st.cache_data(show_spinner=False, ttl=60)
def _load_usage_log_cached(_mtime: float):
    """Load data/usage_log.jsonl. TTL=60s so the admin view auto-refreshes
    within a minute of new events without polling too aggressively."""
    return load_usage_log()


def _usage_token() -> "str | None":
    """Token lookup mirroring the catalog save handlers."""
    try:
        return st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    except Exception:
        return None


def _track_and_flush(event_type: str, *, force_flush: bool = True, **payload) -> None:
    """Convenience wrapper: queue an event, then flush if we have a token.

    Most callers use ``force_flush=True`` because they're already in a save
    context (cheap to piggyback the GitHub round-trip). The session_start
    and schedule_load callers also force-flush so the very first event in a
    session always lands in the log within seconds.
    """
    try:
        track_event(event_type, **payload)
        _flush_usage_to_github(_usage_token(), force=force_flush)
    except Exception:
        # Tracking must never break the user's actual save flow.
        pass




# ---------------------------------------------------------------------------
# Auto-spread (LPT level-loading)
# ---------------------------------------------------------------------------
# Month-label parsers ("May-26" / "26-Apr") for the target-month detection.
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec"]
_CURRENT_MONTH_RE = re.compile(r"^([A-Za-z]{3})-(\d{2})$")
_CARRYOVER_MONTH_RE = re.compile(r"^(\d{2})-([A-Za-z]{3})$")


def _parse_target_month(month_label: str) -> "tuple[int, int] | None":
    """Convert a PRODUCTION MONTH cell value to (year, month_num) or None.

    Accepts the two formats the existing loader produces:
      "May-26"  → (2026, 5)   (current-month style)
      "26-Apr"  → (2026, 4)   (carryover style)
    """
    if not month_label:
        return None
    s = str(month_label).strip()
    m = _CURRENT_MONTH_RE.match(s)
    if m:
        mon_str, yy = m.group(1), m.group(2)
    else:
        m = _CARRYOVER_MONTH_RE.match(s)
        if not m:
            return None
        yy, mon_str = m.group(1), m.group(2)
    try:
        mon_idx = _MONTH_NAMES.index(mon_str.lower()) + 1
        year = 2000 + int(yy)
    except (ValueError, IndexError):
        return None
    return year, mon_idx


def _working_days_in_month(year: int, month: int) -> list[date]:
    """M–F dates in the given month, ordered chronologically.

    No holiday awareness — if the team has a known company holiday, the
    heatmap surfaces the resulting overload and the planner can re-date
    the affected rows by hand.
    """
    last = calendar.monthrange(year, month)[1]
    out = []
    for d in range(1, last + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:  # Monday=0 … Friday=4
            out.append(dt)
    return out


def _row_labor_weight(row: pd.Series, machine_df, acc_df) -> float:
    """Approximate per-row labor (person-minutes × qty).

    Falls back to ``BUILD QTY × 100`` when ``compute_unit_labor`` can't
    resolve the SKU — still gets the row a non-zero weight so LPT can
    place it somewhere reasonable.
    """
    fg = row.get("FG_BASE") or row.get("FG SKU ID")
    acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
    qty = float(row.get("BUILD QTY", 0) or 0)
    try:
        labor = compute_unit_labor(fg, acc, machine_df, acc_df)
        if labor is None:
            return max(qty, 1.0) * 100.0
        per_unit = sum(float(labor.get(s, 0) or 0) for s in STATION_KEYS)
        return max(qty, 1.0) * per_unit
    except Exception:
        return max(qty, 1.0) * 100.0


_MONTH_FROM_NAME = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_month_from_name(label: str) -> "tuple[int, int] | None":
    """Try to extract a (year, month) from a free-form name like
    "Hen 2026 June" or "May-26 plan". Returns None if no month token
    can be found.
    """
    if not label:
        return None
    text = str(label).lower()
    # Find month token
    month = None
    for token, m in _MONTH_FROM_NAME.items():
        # Whole-word match so "marble" doesn't match "mar"
        if re.search(rf"\b{token}\b", text):
            month = m
            break
    if month is None:
        return None
    # Find year token — 4-digit first, then 2-digit (assume 20xx)
    year = None
    y4 = re.search(r"\b(20\d{2})\b", text)
    if y4:
        year = int(y4.group(1))
    else:
        y2 = re.search(r"\b(\d{2})\b", text)
        if y2:
            year = 2000 + int(y2.group(1))
    if year is None:
        year = now_local().year
    return (year, month)


def _auto_spread_dates(
    df: pd.DataFrame, machine_df, acc_df, location: str,
    target_month_hint: "tuple[int, int] | None" = None,
) -> pd.DataFrame:
    """Fill blank PRODUCTION DAY cells with auto-leveled dates.

    Uses the **Longest Processing Time** heuristic for the parallel-
    machine scheduling problem: sort blank rows by labor weight, descending,
    and place each into the working day with the smallest running total.
    Provably gives ≤ 4/3× optimal makespan and runs in O(n log n).

    Mixed-mode aware: rows that already have a valid PRODUCTION DAY are
    preserved, and their labor weight seeds that day's running total so
    the blank rows are level-loaded **on top** of the existing pinned plan.

    Target-month resolution order:
      1. ``target_month_hint`` (callers in manual mode pass this from a
         scenario name like "Hen 2026 June" → (2026, 6)).
      2. First non-null ``PRODUCTION MONTH`` value parseable by
         ``_parse_target_month`` (e.g. "May-26" or "26-Apr").
      3. Fallback: today's month in Las Vegas time.

    Side effects:
      • Sets ``df["PRODUCTION DAY"]`` for previously-blank rows to a real
        ``pd.Timestamp`` in the target month.
      • Recomputes ``df["WEEK_OF_MONTH"]`` for all rows from the new
        PRODUCTION DAY values.
      • Attaches ``df.attrs["auto_spread"]`` = a small dict with metadata
        for the UI to surface: ``{count, total_rows, target_month_label,
        working_days}``.

    Returns the (mutated) df. Guards against the obvious edge cases — empty
    schedule, no working days, undetectable target month — by returning the
    df unchanged with no ``auto_spread`` attribute set.
    """
    if df is None or df.empty:
        return df

    # Identify which rows need a date assigned. Anything that isn't a real
    # Timestamp counts as "blank" (handles NaT, empty strings, etc.).
    if "PRODUCTION DAY" in df.columns:
        day_series = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce")
    else:
        day_series = pd.Series([pd.NaT] * len(df), index=df.index)
    blank_mask = day_series.isna()
    if not blank_mask.any():
        return df  # nothing to do — every row is already dated

    # Detect the target (year, month). Hint wins → schedule's PRODUCTION
    # MONTH → today's month as fallback.
    target = target_month_hint
    if target is None and "PRODUCTION MONTH" in df.columns:
        for v in df["PRODUCTION MONTH"].dropna():
            target = _parse_target_month(v)
            if target:
                break
    if target is None:
        _now = now_local()
        target = (_now.year, _now.month)
    year, month = target

    working = _working_days_in_month(year, month)
    if not working:
        return df  # defensive — never happens for real months

    # Map each working day to its week-of-month bucket so we can balance
    # load at the WEEK level first (so a small schedule doesn't bunch into
    # the first few days), then break ties at the day level.
    day_to_week: dict[date, int] = {d: week_of_month(d) for d in working}
    weeks_set = sorted(set(day_to_week.values()))

    # Seed running loads from rows that ARE already dated.
    day_load: dict[date, float] = {d: 0.0 for d in working}
    week_load: dict[int, float] = {w: 0.0 for w in weeks_set}
    for idx in df.index[~blank_mask]:
        d = day_series.loc[idx].date()
        if d in day_load:
            w = _row_labor_weight(df.loc[idx], machine_df, acc_df)
            day_load[d] += w
            week_load[day_to_week[d]] = week_load.get(day_to_week[d], 0.0) + w
        # If a pinned date falls outside the target month's working days,
        # we don't add it to any bucket — auto-spread still works, the
        # heatmap just won't show that pin.

    # Compute labor weights for the blank rows once, then walk them
    # largest-first and drop each into the lightest-loaded (week, day).
    # The (week_load, day_load) key means a 5-unit plan spreads as
    # 1-per-week instead of clustering in the first ~5 days.
    blank_idxs = list(df.index[blank_mask])
    weights = {idx: _row_labor_weight(df.loc[idx], machine_df, acc_df)
               for idx in blank_idxs}
    blank_idxs.sort(key=lambda i: weights[i], reverse=True)

    assigned: dict = {}
    for idx in blank_idxs:
        d = min(
            working,
            key=lambda x: (week_load[day_to_week[x]], day_load[x]),
        )
        assigned[idx] = d
        day_load[d] += weights[idx]
        week_load[day_to_week[d]] += weights[idx]

    # Write the new dates back. Preserve the pinned rows untouched.
    new_days = day_series.copy()
    for idx, d in assigned.items():
        new_days.loc[idx] = pd.Timestamp(d)
    df["PRODUCTION DAY"] = new_days

    # Recompute WEEK_OF_MONTH from the (now-complete) PRODUCTION DAY.
    df["WEEK_OF_MONTH"] = week_of_month_series(new_days)

    df.attrs["auto_spread"] = {
        "count": int(blank_mask.sum()),
        "total_rows": int(len(df)),
        "target_month_label": f"{calendar.month_name[month]} {year}",
        "working_days": len(working),
    }
    return df


def _suggest_schedule_dates(
    schedule_df: pd.DataFrame, machine_df, acc_df, inputs: dict,
) -> "tuple[pd.DataFrame, list[dict]]":
    """Constraint-aware version of auto-spread.

    Different from ``_auto_spread_dates`` in three ways:
      1. **Overrides** existing PRODUCTION DAY values — the planner clicked
         "Suggest" so they want a fresh re-spread, not an additive one.
      2. **Per-station capacity check** — for each candidate day, the placer
         verifies that putting this unit's labor into that week wouldn't
         tip any station above ``UTIL_THRESHOLD_OVER`` (1.0). If every day
         fails, the unit lands on the least-loaded day and the offending
         station(s) are recorded in the returned overload list.
      3. **Returns a new DataFrame** rather than mutating the original, so
         the UI can preview the proposal alongside the current schedule.

    Returns ``(new_schedule_df, overloads)`` where ``overloads`` is a list
    of dicts: ``{"row_idx", "fg", "week", "stations"}``.
    """
    if schedule_df is None or schedule_df.empty:
        return schedule_df, []

    new_df = schedule_df.copy()

    # ---- Target month detection ----------------------------------------
    # The sidebar "Plan month" picker is authoritative (same as auto-spread),
    # so the suggestion level-loads across the month the planner chose — not
    # whatever the CSV's PRODUCTION MONTH happens to say. Fall back to the
    # CSV month, then today, only if no plan month is set.
    target = inputs.get("plan_month") if isinstance(inputs, dict) else None
    if target is None and "PRODUCTION MONTH" in new_df.columns:
        for v in new_df["PRODUCTION MONTH"].dropna():
            target = _parse_target_month(v)
            if target:
                break
    if target is None:
        _now = now_local()
        target = (_now.year, _now.month)
    year, month = target

    working = _working_days_in_month(year, month)
    if not working:
        return new_df, []

    # ---- Per-station weekly capacity (5-day reference) ------------------
    from core.constants import STATION_KEY_TO_DISPLAY, UTIL_THRESHOLD_OVER
    crew_config = inputs["crew_config"]
    shift = float(inputs["shift"])
    eff = float(inputs["efficiency"])
    safety = float(inputs["safety"])
    week_days = 5

    station_capacity: dict[str, float] = {}
    for st_key in STATION_KEYS:
        st_disp = STATION_KEY_TO_DISPLAY.get(st_key)
        if not st_disp or st_disp not in crew_config.index:
            station_capacity[st_key] = 0.0
            continue
        try:
            hc = int(crew_config.loc[st_disp, "HC"])
        except Exception:
            hc = 0
        station_capacity[st_key] = (
            hc * shift * week_days * eff * safety if hc > 0 else 0.0
        )

    # Map each working day → its week
    day_to_week = {d: week_of_month(d) for d in working}
    weeks = sorted(set(day_to_week.values()))

    # Running per-station-per-week load + total per-day load + total
    # per-week load. The week-level total is what lets us spread a small
    # plan across all 4-5 weeks instead of clustering everything in the
    # first few days (LPT minimizes makespan; balancing weeks first then
    # days within a week gives the level-loaded heatmap planners expect).
    station_load: dict = {(st, w): 0.0 for st in STATION_KEYS for w in weeks}
    day_total_load: dict = {d: 0.0 for d in working}
    week_total_load: dict = {w: 0.0 for w in weeks}

    # Per-row labor breakdown
    def _row_breakdown(row) -> "tuple[dict, float]":
        fg = row.get("FG_BASE") or row.get("FG SKU ID")
        acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
        try:
            qty = float(row.get("BUILD QTY", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            labor = compute_unit_labor(fg, acc, machine_df, acc_df)
            if labor is None:
                return {}, max(qty, 1.0) * 100.0
            breakdown = {
                s: float(labor.get(s, 0) or 0) * qty for s in STATION_KEYS
            }
            total = sum(breakdown.values())
            return breakdown, total
        except Exception:
            return {}, max(qty, 1.0) * 100.0

    # Compute all breakdowns up front and sort largest-first (LPT)
    rows = []
    for idx in new_df.index:
        br, total = _row_breakdown(new_df.loc[idx])
        rows.append((idx, br, total))
    rows.sort(key=lambda r: r[2], reverse=True)

    overloads: list[dict] = []
    assigned: dict = {}
    for idx, breakdown, total in rows:
        # Walk candidate days lightest-first, where "light" means the
        # day's *week* has the least load. Within a week, prefer the
        # lightest day. Without the week-level key, a small plan would
        # cluster into the first few days because LPT optimizes makespan
        # per day, not balance per week.
        candidates = sorted(
            working,
            key=lambda d: (week_total_load[day_to_week[d]], day_total_load[d]),
        )
        placed_day = None
        for cand in candidates:
            w = day_to_week[cand]
            ok = True
            for st_key, cost in breakdown.items():
                if cost <= 0:
                    continue
                cap = station_capacity.get(st_key, 0.0)
                if cap <= 0:
                    # Station doesn't exist here — skip the constraint check
                    # (treat it like infinite capacity for this purpose).
                    continue
                if (station_load[(st_key, w)] + cost) / cap > UTIL_THRESHOLD_OVER:
                    ok = False
                    break
            if ok:
                placed_day = cand
                break

        if placed_day is None:
            # No day satisfies the per-station check. Fall back to the
            # least-loaded week+day pair and record which stations are
            # going to be tipped over (same ordering as the constraint
            # search, so the fallback respects week-balance too).
            placed_day = min(
                working,
                key=lambda d: (week_total_load[day_to_week[d]], day_total_load[d]),
            )
            w = day_to_week[placed_day]
            offenders = []
            for st_key, cost in breakdown.items():
                if cost <= 0:
                    continue
                cap = station_capacity.get(st_key, 0.0)
                if cap > 0 and (station_load[(st_key, w)] + cost) / cap > UTIL_THRESHOLD_OVER:
                    offenders.append(st_key)
            if offenders:
                fg_label = (
                    new_df.loc[idx].get("FG_BASE")
                    or new_df.loc[idx].get("FG SKU ID")
                    or "?"
                )
                overloads.append({
                    "row_idx": idx,
                    "fg": str(fg_label),
                    "week": int(w),
                    "stations": offenders,
                })

        # Commit the placement
        assigned[idx] = placed_day
        day_total_load[placed_day] += total
        wk = day_to_week[placed_day]
        week_total_load[wk] = week_total_load.get(wk, 0.0) + total
        for st_key, cost in breakdown.items():
            station_load[(st_key, wk)] = station_load.get((st_key, wk), 0.0) + cost

    # ---- Write the proposed dates back ---------------------------------
    new_days = pd.Series(
        [pd.Timestamp(assigned[i]) for i in new_df.index],
        index=new_df.index,
        dtype="datetime64[ns]",
    )
    new_df["PRODUCTION DAY"] = new_days
    new_df["WEEK_OF_MONTH"] = week_of_month_series(new_days)
    new_df.attrs["suggested"] = {
        "target_month_label": f"{calendar.month_name[month]} {year}",
        "working_days": len(working),
        "total_rows": int(len(new_df)),
        "overloads_count": len(overloads),
    }
    return new_df, overloads


def _diagnose_overloads(
    schedule_df: pd.DataFrame,
    machine_df,
    acc_df,
    inputs: dict,
    overloads: "list[dict]",
) -> "list[dict]":
    """Turn an overload list into actionable diagnoses.

    For each station that's binding (i.e. some unit couldn't fit there at
    safe capacity), compute:
      • monthly demand at that station (p-min)
      • monthly capacity at the current settings (p-min)
      • shortfall (demand − capacity, p-min)
      • three concrete fixes:
          1. HC to add — ``ceil(shortfall / (shift × days × eff × safety))``
          2. Extra working days needed
          3. Which heaviest units to defer to free up the shortfall

    Returns a list of dicts (one per binding station), sorted by severity:
    ``[{station, demand, capacity, shortfall, shortfall_pct,
        hire_hc, extend_days, defer_units}, …]``.

    Returns ``[]`` when ``overloads`` is empty (sanity guard).
    """
    if not overloads or schedule_df is None or schedule_df.empty:
        return []
    from core.constants import STATION_KEY_TO_DISPLAY

    crew_config = inputs["crew_config"]
    shift = float(inputs["shift"])
    eff = float(inputs["efficiency"])
    safety = float(inputs["safety"])
    days = float(inputs["days"])

    # 1) Identify the set of stations called out as binding.
    binding_stations: set = set()
    for o in overloads:
        for s in o.get("stations", []):
            binding_stations.add(s)
    if not binding_stations:
        return []

    # 2) Compute monthly demand + capacity per binding station.
    diagnoses: list[dict] = []
    for st_key in binding_stations:
        # Sum p-min of this station across the whole schedule.
        demand = 0.0
        for _, row in schedule_df.iterrows():
            try:
                fg = row.get("FG_BASE") or row.get("FG SKU ID")
                acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
                qty = float(row.get("BUILD QTY", 0) or 0)
                labor = compute_unit_labor(fg, acc, machine_df, acc_df)
                if labor is None:
                    continue
                demand += float(labor.get(st_key, 0) or 0) * qty
            except Exception:
                continue
        if demand <= 0:
            continue

        st_disp = STATION_KEY_TO_DISPLAY.get(st_key, st_key)
        if st_disp not in crew_config.index:
            continue
        try:
            hc = int(crew_config.loc[st_disp, "HC"])
        except Exception:
            hc = 0

        capacity = hc * shift * days * eff * safety if hc > 0 else 0.0
        shortfall = max(demand - capacity, 0.0)
        if shortfall <= 0:
            continue  # not actually short at the monthly level
        shortfall_pct = (demand / capacity - 1.0) if capacity > 0 else float("inf")

        # 3a) HC to add: ceil(shortfall / per-HC monthly cap)
        per_hc_month = shift * days * eff * safety
        hire_hc = int((shortfall + per_hc_month - 1) // per_hc_month) if per_hc_month > 0 else 0

        # 3b) Extra working days: ceil(shortfall / per-day cap)
        per_day_cap = hc * shift * eff * safety if hc > 0 else 0.0
        extend_days = int((shortfall + per_day_cap - 1) // per_day_cap) if per_day_cap > 0 else 0

        # 3c) Heaviest units to defer: pick FG SKUs by descending labor at
        # this station × qty, accumulate until shortfall is covered.
        per_row_labor: list[tuple] = []
        for idx, row in schedule_df.iterrows():
            try:
                fg = row.get("FG_BASE") or row.get("FG SKU ID")
                acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
                qty = float(row.get("BUILD QTY", 0) or 0)
                labor = compute_unit_labor(fg, acc, machine_df, acc_df)
                if labor is None:
                    continue
                row_total = float(labor.get(st_key, 0) or 0) * qty
                if row_total > 0:
                    per_row_labor.append((row_total, str(fg), int(qty)))
            except Exception:
                continue
        per_row_labor.sort(reverse=True)
        defer_units: list[dict] = []
        freed = 0.0
        for total, fg, qty in per_row_labor:
            if freed >= shortfall:
                break
            defer_units.append({"fg": fg, "qty": qty, "labor_at_station": total})
            freed += total

        diagnoses.append({
            "station_key": st_key,
            "station_display": st_disp,
            "demand": demand,
            "capacity": capacity,
            "shortfall": shortfall,
            "shortfall_pct": shortfall_pct,
            "hire_hc": hire_hc,
            "extend_days": extend_days,
            "defer_units": defer_units,
            "freed_by_defer": freed,
        })

    # Sort severity (largest shortfall first)
    diagnoses.sort(key=lambda d: d["shortfall"], reverse=True)
    return diagnoses


def _upload_sig(uploaded_file) -> str:
    """Stable identity for a Streamlit ``UploadedFile`` so we can tell a
    genuinely new upload from the same file persisting across reruns.

    Prefers Streamlit's per-upload ``file_id``; falls back to name + size.
    """
    if uploaded_file is None:
        return ""
    fid = getattr(uploaded_file, "file_id", None)
    if fid:
        return str(fid)
    return f"{getattr(uploaded_file, 'name', '')}:{getattr(uploaded_file, 'size', '')}"


def _load_schedule_df(
    uploaded_file=None, location: str = "HENDERSON",
    target_month_hint: "tuple[int, int] | None" = None,
) -> pd.DataFrame:
    """Schedule loader — accepts an uploaded CSV, a saved-schedule buffer, or
    falls back to the bundled May 2026 sample.

    Resolution order:
      1. A one-shot ``st.session_state['_loaded_schedule_buffer']`` — produced
         by "📂 Load saved schedule", a Sequencer Accept, a 🗓 Sequence Apply,
         a Bulk Move, or a 🧹 Suffix Strip. Consumed once, then **promoted** to
         the persistent active schedule (below).
      2. A genuinely new ``uploaded_file`` from ``st.file_uploader`` (detected
         via :func:`_upload_sig`). Becomes the persistent active schedule.
      3. The persistent ``st.session_state['_active_schedule_csv']`` — whatever
         was loaded last. This is what lets the schedule survive reruns from
         unrelated widgets (e.g. the efficiency slider) instead of resetting.
      4. The bundled ``data/may_schedule.csv``.

    Passes file-like content as ``io.BytesIO`` so no temp file is written to
    disk — protects against race conditions when multiple users upload
    simultaneously and preserves the privacy guarantee that browser-only
    uploads stay browser-only unless the planner explicitly saves them.

    On a successful load the function stashes a tiny summary dict in
    ``st.session_state['_last_upload_summary']`` so the sidebar can render
    a "✅ Loaded N rows from X, month Y" confirmation.

    Re-raises ``ScheduleColumnError`` so the caller can surface a clean,
    planner-friendly message via ``st.warning``.
    """
    machine_skus = set(_load_machine_df(_csv_mtime("machine_clean.csv"))["SKU"])

    # Auto-spread needs the full machine + accessory frames so it can weight
    # each row by labor (per-station p-min × qty) before running LPT. Both
    # come from the same cached loaders the rest of main() uses.
    machine_df_full = _load_machine_df(_csv_mtime("machine_clean.csv"))
    acc_df_full = _load_acc_df(_csv_mtime("acc_clean.csv"))

    # The Plan-month picker is authoritative — show it on the confirmation pill
    # (and use it for auto-spread) rather than the CSV's own PRODUCTION MONTH.
    _plan_label = ""
    if target_month_hint:
        _py, _pm = target_month_hint
        _plan_label = f"{calendar.month_name[_pm]} {_py}"

    def _finalize(df: pd.DataFrame, source: str) -> pd.DataFrame:
        """Common post-load step: auto-spread blank PRODUCTION DAY rows so
        the weekly heatmap has something to chart, then stash the summary."""
        if df is not None and not df.empty:
            _auto_spread_dates(
                df, machine_df_full, acc_df_full, location,
                target_month_hint=target_month_hint,
            )
        _stash_upload_summary(df, location, source=source, plan_month_label=_plan_label)
        return df

    # Resolution order. NOTE: the previously-loaded schedule must survive
    # unrelated reruns (e.g. the planner nudging the efficiency slider). We
    # persist the active schedule's CSV bytes in ``_active_schedule_csv`` so a
    # rerun re-resolves to the *same* schedule instead of falling back to the
    # uploader / bundled sample — that fallback was the "save schedule keeps
    # resetting" bug.

    # 1) One-shot action buffer — a saved-schedule load, a Sequencer Accept,
    #    a 🗓 Sequence Apply, a Bulk Move, or a 🧹 Suffix Strip just happened.
    #    Consume it and promote it to the persistent active schedule.
    buf = st.session_state.pop("_loaded_schedule_buffer", None)
    if buf is not None:
        csv_bytes = buf.getvalue()
        st.session_state["_active_schedule_csv"] = csv_bytes
        st.session_state["_active_schedule_source"] = st.session_state.pop(
            "_loaded_schedule_source", "saved"
        )
        # A programmatic load supersedes whatever file is still sitting in the
        # uploader widget — mark it "seen" so the next rerun doesn't treat it
        # as a fresh upload and clobber what we just loaded.
        if uploaded_file is not None:
            st.session_state["_seen_upload_sig"] = _upload_sig(uploaded_file)
        df = load_schedule(
            io.BytesIO(csv_bytes), location=location, machine_skus=machine_skus,
        )
        return _finalize(df, st.session_state["_active_schedule_source"])

    # 2) Live file upload — but only treat it as authoritative when it's a
    #    *new* file. The uploader keeps returning the same object across
    #    reruns, so without the signature check a stale upload would override
    #    a saved schedule the planner loaded afterwards.
    if uploaded_file is not None:
        sig = _upload_sig(uploaded_file)
        if sig != st.session_state.get("_seen_upload_sig"):
            st.session_state["_seen_upload_sig"] = sig
            csv_bytes = uploaded_file.getvalue()
            st.session_state["_active_schedule_csv"] = csv_bytes
            st.session_state["_active_schedule_source"] = "upload"
            df = load_schedule(
                io.BytesIO(csv_bytes), location=location, machine_skus=machine_skus,
            )
            return _finalize(df, "upload")
        # Same file as before → fall through to the persisted active schedule.

    # 3) Persisted active schedule — survives reruns from slider/widget changes.
    active = st.session_state.get("_active_schedule_csv")
    if active is not None:
        df = load_schedule(
            io.BytesIO(active), location=location, machine_skus=machine_skus,
        )
        return _finalize(df, st.session_state.get("_active_schedule_source", "saved"))

    # 4) Bundled sample
    df = load_schedule(location=location, machine_skus=machine_skus)
    return _finalize(df, "bundled")


def _stash_upload_summary(df: pd.DataFrame, location: str, source: str,
                          plan_month_label: str = "") -> None:
    """Save a one-line description of the just-loaded schedule into session
    state so the sidebar can echo it back to the planner as a green
    confirmation pill. No-op when the DataFrame is empty (caller already
    surfaces an empty-state banner).

    ``plan_month_label`` is the authoritative Plan-month chosen in the sidebar
    (e.g. "June 2026"). When set it's what the pill shows, since that month
    drives the Calendar + auto-spread — even if the CSV's own PRODUCTION MONTH
    column says something else (which we keep as ``csv_month`` for context)."""
    try:
        if df is None or df.empty:
            st.session_state.pop("_last_upload_summary", None)
            return
        months = (
            df.loc[~df["CARRYOVER"], "PRODUCTION MONTH"].unique().tolist()
            if "CARRYOVER" in df.columns else []
        )
        csv_month = months[0] if months else (
            df["PRODUCTION MONTH"].iloc[0] if "PRODUCTION MONTH" in df.columns else ""
        )
        st.session_state["_last_upload_summary"] = {
            "rows": int(len(df)),
            "location": str(location),
            "month": str(plan_month_label or csv_month or "—"),
            "csv_month": str(csv_month or ""),
            "source": source,
            "auto_spread": (df.attrs.get("auto_spread")
                            if hasattr(df, "attrs") else None),
        }
    except Exception:
        st.session_state.pop("_last_upload_summary", None)


# =============================================================
# Sequencer — Suggest button + Accept/Discard proposal flow
# =============================================================
def _render_sequencer_controls(
    schedule_df: pd.DataFrame, machine_df, acc_df, inputs: dict,
) -> pd.DataFrame:
    """Top-of-page control bar for the Sequencer.

    Provides a 💡 Suggest button (level-loaded re-spread that respects
    per-station weekly capacity) plus accept/discard buttons when a
    proposal is pending. Returns the schedule_df that should drive the
    rest of main() — either the original, or the accepted proposal.

    Pending proposal state lives in ``st.session_state["_proposed_schedule"]``
    and ``st.session_state["_proposed_schedule_overloads"]``.
    """
    if schedule_df is None or schedule_df.empty:
        return schedule_df

    # If a proposal is pending, render the accept/discard bar and the
    # diagnosis. Otherwise render the Suggest trigger.
    proposal = st.session_state.get("_proposed_schedule")
    overloads = st.session_state.get("_proposed_schedule_overloads", [])

    if isinstance(proposal, pd.DataFrame) and not proposal.empty:
        meta = proposal.attrs.get("suggested", {})
        with st.container():
            st.info(
                f"💡 **Proposed level-loaded schedule** — {meta.get('total_rows', '?')} "
                f"units across {meta.get('working_days', '?')} working days of "
                f"**{meta.get('target_month_label', '?')}**. "
                "Review the comparison heatmap on the **🏠 Overview** tab. "
                "Accept to switch the analysis to this sequence, or Discard to keep your current one."
            )
            if overloads:
                _ol_summary = ", ".join(
                    f"{o['fg']} (Wk {o['week']}: {', '.join(o['stations'])})"
                    for o in overloads[:5]
                )
                if len(overloads) > 5:
                    _ol_summary += f" … (+{len(overloads) - 5} more)"
                st.warning(
                    f"⚠️ **{len(overloads)} unit(s) couldn't fit without overload:** "
                    f"{_ol_summary}."
                )

                # Real-talk diagnosis: name the binding station(s),
                # quantify the gap, propose concrete fixes.
                diagnoses = _diagnose_overloads(
                    proposal, machine_df, acc_df, inputs, overloads,
                )
                if diagnoses:
                    st.markdown("##### 🛠 How to fix this")
                    for d in diagnoses:
                        with st.container():
                            st.markdown(
                                f"**Bottleneck: {d['station_display']}** "
                                f"— short **{int(d['shortfall']):,} p-min** "
                                f"({d['shortfall_pct']*100:+.0f}% over capacity)"
                            )
                            st.caption(
                                f"Monthly demand: **{int(d['demand']):,} p-min** "
                                f"· Capacity at current settings: "
                                f"**{int(d['capacity']):,} p-min**"
                            )

                            colA, colB, colC = st.columns(3)
                            with colA:
                                st.markdown(
                                    f"🧑 **Hire** &nbsp; +{d['hire_hc']} HC"
                                )
                                st.caption(
                                    f"at {d['station_display']}. Bring weekly "
                                    "capacity up by enough to absorb the gap."
                                )
                            with colB:
                                st.markdown(
                                    f"📅 **Extend** &nbsp; +{d['extend_days']} day(s)"
                                )
                                st.caption(
                                    "Run overtime / Saturdays. Keeps current HC."
                                )
                            with colC:
                                defer_label = "🚚 **Defer** "
                                if d["defer_units"]:
                                    deferred_total = sum(u["qty"] for u in d["defer_units"])
                                    defer_label += f"&nbsp; {deferred_total} unit(s)"
                                    st.markdown(defer_label)
                                    _names = ", ".join(
                                        f"{u['fg']} ×{u['qty']}"
                                        for u in d["defer_units"][:5]
                                    )
                                    if len(d["defer_units"]) > 5:
                                        _names += f" … (+{len(d['defer_units']) - 5} more)"
                                    st.caption(
                                        f"Push heaviest units to next month: "
                                        f"{_names}. Frees "
                                        f"~{int(d['freed_by_defer']):,} p-min."
                                    )
                                else:
                                    st.markdown(defer_label + "&nbsp; —")
                                    st.caption("No clear deferral candidates.")
                            st.markdown("")  # spacer between stations
                    st.caption(
                        "Pick one of the three options above, change the "
                        "matching sidebar setting (or the schedule), and "
                        "click **💡 Generate proposal** again. "
                        "Or hit ✅ Accept to live with the overload."
                    )
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("✅ Accept proposal", use_container_width=True, type="primary"):
                # Replace the working schedule and clear proposal state.
                accepted = st.session_state.pop("_proposed_schedule")
                st.session_state.pop("_proposed_schedule_overloads", None)
                # Push the accepted CSV through the upload buffer so the
                # standard loader path picks it up on the next rerun.
                csv_text = accepted.to_csv(index=False)
                st.session_state["_loaded_schedule_buffer"] = io.BytesIO(
                    csv_text.encode("utf-8")
                )
                _track_and_flush(
                    "sequencer_accept",
                    facility=inputs.get("location"),
                    rows=int(len(accepted)),
                    overloads=len(overloads),
                )
                st.success("Proposal accepted — refreshing the analysis…")
                st.rerun()
            if c2.button("🚫 Discard", use_container_width=True):
                st.session_state.pop("_proposed_schedule", None)
                st.session_state.pop("_proposed_schedule_overloads", None)
                _track_and_flush("sequencer_discard", facility=inputs.get("location"))
                st.rerun()
        return schedule_df

    # No proposal pending — show the Suggest trigger inside an expander so
    # it's discoverable but doesn't dominate the page.
    with st.expander("💡 Suggest a level-loaded schedule", expanded=False):
        st.caption(
            "Re-spreads every unit across this month's working days using the "
            "**Longest Processing Time** heuristic, with a per-station weekly "
            "capacity check so no station goes over 100% in any one week. "
            "Existing PRODUCTION DAY values are **overridden** — the planner "
            "explicitly asked for a fresh sequence. You'll review a side-by-"
            "side preview before committing."
        )
        if st.button(
            "💡 Generate proposal",
            use_container_width=True,
            help=(
                "Runs the constraint-aware LPT scheduler on your current "
                "schedule. Result shows on the Overview tab as a side-by-side "
                "heatmap with accept/discard buttons."
            ),
        ):
            with st.spinner("Running constraint-aware LPT scheduler…"):
                try:
                    new_df, ol = _suggest_schedule_dates(
                        schedule_df, machine_df, acc_df, inputs,
                    )
                    st.session_state["_proposed_schedule"] = new_df
                    st.session_state["_proposed_schedule_overloads"] = ol
                    _track_and_flush(
                        "sequencer_suggest",
                        facility=inputs.get("location"),
                        rows=int(len(new_df)),
                        overloads=len(ol),
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Suggester failed: {e}")
    return schedule_df


def _render_sequencer_view(schedule_df: pd.DataFrame) -> None:
    """The 🗓 Sequence sub-section under Data & Setup.

    Two side-by-side controls that together form the manual-reshape UX:
      • Editable table with PRODUCTION DAY as a date-picker column. Edit
        rows inline, click Apply to write back into the session schedule.
      • Bulk move form: shift N units of SKU X from Week A → Week B in
        one operation.

    Both write the reshaped CSV through ``st.session_state["_loaded_schedule_buffer"]``
    so the standard loader picks it up on the next rerun and the analysis
    tabs immediately reflect the new sequence.
    """
    st.subheader("🗓 Sequence — manual reshape")
    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then come "
            "back here to reshape the dates."
        )
        return

    st.caption(
        "Edit individual `PRODUCTION DAY` cells in the table, OR use the "
        "**Bulk move** form below to shift many units at once. Changes "
        "live in your browser session — use **📥 Download** to save a copy "
        "you can re-upload later (or save via the sidebar's 📂 Save panel)."
    )

    # Reset button — toss any reshape edits so the table reflects the
    # canonical schedule again.
    if st.button("↩️ Revert to current sequence", help="Drops any unsaved reshape edits."):
        st.session_state.pop("_sequencer_edits", None)
        st.rerun()

    # Choose which columns to expose in the editor. Keep the editable surface
    # narrow (date + qty) so we don't accidentally let the planner mangle
    # the FG/Accessory SKUs from this view (those have their own catalogs).
    display_cols = [
        c for c in ["FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY",
                    "PRODUCTION DAY", "WEEK_OF_MONTH",
                    "CUSTOMER NAME", "PRODUCTION MONTH", "LOCATION"]
        if c in schedule_df.columns
    ]
    work = schedule_df[display_cols].copy()
    # Make sure PRODUCTION DAY is a real datetime so st.column_config.DateColumn
    # gets it as a Date.
    if "PRODUCTION DAY" in work.columns:
        work["PRODUCTION DAY"] = pd.to_datetime(work["PRODUCTION DAY"], errors="coerce")

    col_cfg = {}
    if "FG SKU ID" in work.columns:
        col_cfg["FG SKU ID"] = st.column_config.TextColumn("FG SKU", disabled=True)
    if "FG ACCRY SKU ID" in work.columns:
        col_cfg["FG ACCRY SKU ID"] = st.column_config.TextColumn(
            "Accessory SKU", disabled=True,
        )
    if "BUILD QTY" in work.columns:
        col_cfg["BUILD QTY"] = st.column_config.NumberColumn(
            "Qty", min_value=0, step=1,
            help="Edit if you want to scale a row up/down.",
        )
    if "PRODUCTION DAY" in work.columns:
        col_cfg["PRODUCTION DAY"] = st.column_config.DateColumn(
            "Production Day",
            help="Click the cell and pick a new date.",
        )
    if "WEEK_OF_MONTH" in work.columns:
        col_cfg["WEEK_OF_MONTH"] = st.column_config.NumberColumn(
            "Week", disabled=True,
            help="Derived from Production Day after you click Apply.",
        )
    if "CUSTOMER NAME" in work.columns:
        col_cfg["CUSTOMER NAME"] = st.column_config.TextColumn(
            "Customer", disabled=True,
        )
    if "PRODUCTION MONTH" in work.columns:
        col_cfg["PRODUCTION MONTH"] = st.column_config.TextColumn(
            "Prod Month", disabled=True,
        )
    if "LOCATION" in work.columns:
        col_cfg["LOCATION"] = st.column_config.TextColumn(
            "Location", disabled=True,
        )

    edited = st.data_editor(
        work,
        use_container_width=True,
        num_rows="fixed",
        column_config=col_cfg,
        key="sequencer_editor",
        height=480,
    )

    # Apply / Download
    ca, cb = st.columns([1, 1])
    if ca.button(
        "🔄 Apply edits",
        use_container_width=True,
        type="primary",
        help=(
            "Writes the edited rows back into the session schedule so all "
            "analysis tabs use the new sequence."
        ),
    ):
        _apply_sequencer_edits(schedule_df, edited)

    # Build the downloadable CSV with the edits applied (without committing
    # to session state until Apply is clicked).
    try:
        download_df = _merge_sequencer_edits_into_full(schedule_df, edited)
        cb.download_button(
            "📥 Download reshaped schedule",
            download_df.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_reshaped_{today_local_str()}.csv",
            mime="text/csv",
            use_container_width=True,
            help=(
                "Saves the current view (including unsaved edits) as a CSV "
                "you can re-upload or save via the 📂 Save panel."
            ),
        )
    except Exception:
        # If merging fails for any reason, fall back to the canonical schedule.
        cb.download_button(
            "📥 Download current schedule",
            schedule_df.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_{today_local_str()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.markdown("---")
    _render_bulk_move_form(schedule_df)


def _merge_sequencer_edits_into_full(
    base_df: pd.DataFrame, edited: pd.DataFrame,
) -> pd.DataFrame:
    """Overlay the columns the editor allowed onto ``base_df`` and return
    the result. Used both by the Apply path and the Download button."""
    out = base_df.copy()
    overlay_cols = [
        c for c in ["PRODUCTION DAY", "BUILD QTY"]
        if c in edited.columns and c in out.columns
    ]
    for c in overlay_cols:
        out[c] = edited[c].values  # editor row-count is fixed; alignment is safe
    # Recompute WEEK_OF_MONTH from the new PRODUCTION DAY
    if "PRODUCTION DAY" in out.columns:
        new_days = pd.to_datetime(out["PRODUCTION DAY"], errors="coerce")
        out["PRODUCTION DAY"] = new_days
        out["WEEK_OF_MONTH"] = week_of_month_series(new_days)
    return out


def _apply_sequencer_edits(base_df: pd.DataFrame, edited: pd.DataFrame) -> None:
    """Commit the editor's changes back into the session schedule.

    The mechanism: serialize the merged DataFrame to CSV bytes and stash
    them in ``_loaded_schedule_buffer`` — the same one-shot path the
    Sequencer's Accept proposal flow uses. Next rerun, the standard loader
    picks the buffer up and the analysis tabs pick up the new dates.
    """
    try:
        merged = _merge_sequencer_edits_into_full(base_df, edited)
        csv_text = merged.to_csv(index=False)
        st.session_state["_loaded_schedule_buffer"] = io.BytesIO(
            csv_text.encode("utf-8")
        )
        _track_and_flush(
            "sequencer_apply",
            rows=int(len(merged)),
        )
        st.success("Edits applied — refreshing the analysis…")
        st.rerun()
    except Exception as e:
        st.error(f"❌ Couldn't apply edits: {e}")


def _render_bulk_move_form(schedule_df: pd.DataFrame) -> None:
    """Bulk move N units of an FG SKU from one week to another."""
    st.markdown("**🔀 Bulk move**")
    st.caption(
        "Shift a batch of units of the same FG SKU from one week to "
        "another. Useful when you want to even out a single problem week "
        "without editing rows one at a time."
    )

    if "WEEK_OF_MONTH" not in schedule_df.columns:
        st.info("Week info isn't available — your schedule has no PRODUCTION DAY.")
        return

    weeks = sorted(
        int(w) for w in schedule_df["WEEK_OF_MONTH"].dropna().unique()
    )
    if len(weeks) < 1:
        st.info("No week-bucketed rows to move.")
        return

    skus = sorted(schedule_df.get("FG SKU ID", pd.Series(dtype=str))
                  .astype(str).str.strip().unique())
    if not skus:
        return

    with st.form("bulk_move_form", clear_on_submit=False):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        chosen_sku = c1.selectbox("FG SKU", options=skus, key="bulk_move_sku")
        from_week = c2.selectbox(
            "From week", options=weeks, key="bulk_move_from",
        )
        to_week_opts = sorted(set(weeks + [w for w in (1, 2, 3, 4, 5) if w not in weeks]))
        to_week = c3.selectbox(
            "To week", options=to_week_opts, key="bulk_move_to",
        )
        qty = c4.number_input(
            "Units", min_value=1, step=1, value=1, key="bulk_move_qty",
        )
        submitted = st.form_submit_button(
            "🔀 Move",
            use_container_width=True,
        )

    if not submitted:
        return

    # Pick the matching rows in the source week — newest first by index so
    # the move targets the bottom of that week's stack.
    mask = (
        (schedule_df["FG SKU ID"].astype(str).str.strip() == chosen_sku)
        & (schedule_df["WEEK_OF_MONTH"] == from_week)
    )
    eligible = schedule_df[mask].copy()
    if eligible.empty:
        st.warning(f"No units of **{chosen_sku}** found in week {from_week}.")
        return

    # Per-row BUILD QTY can be >1, so we accumulate rows until we cover the
    # requested move count, splitting a row when partial.
    moved = 0
    new_df = schedule_df.copy()
    new_rows = []  # for any split (partial-move) cases
    for idx, row in eligible.iterrows():
        if moved >= qty:
            break
        row_qty = int(row.get("BUILD QTY", 0) or 0)
        if row_qty <= 0:
            continue
        take = min(row_qty, qty - moved)
        if take == row_qty:
            # Move the whole row by updating its PRODUCTION DAY to the
            # first working day of the target week.
            new_df.at[idx, "PRODUCTION DAY"] = _first_day_of_week(
                new_df.at[idx, "PRODUCTION DAY"], to_week,
            )
            new_df.at[idx, "WEEK_OF_MONTH"] = to_week
        else:
            # Split: keep `row_qty - take` in the original row, add a new
            # row with `take` units dated to the target week.
            new_df.at[idx, "BUILD QTY"] = row_qty - take
            split_row = row.copy()
            split_row["BUILD QTY"] = take
            split_row["PRODUCTION DAY"] = _first_day_of_week(
                row.get("PRODUCTION DAY"), to_week,
            )
            split_row["WEEK_OF_MONTH"] = to_week
            new_rows.append(split_row)
        moved += take

    if new_rows:
        new_df = pd.concat([new_df, pd.DataFrame(new_rows)], ignore_index=True)

    if moved == 0:
        st.warning(f"Nothing moved — couldn't find {qty} unit(s) of {chosen_sku}.")
        return

    # Commit through the same buffer the editor uses.
    csv_text = new_df.to_csv(index=False)
    st.session_state["_loaded_schedule_buffer"] = io.BytesIO(
        csv_text.encode("utf-8")
    )
    _track_and_flush(
        "sequencer_bulk_move",
        sku=chosen_sku, from_week=from_week, to_week=to_week, units=moved,
    )
    st.success(
        f"🔀 Moved **{moved}** unit(s) of **{chosen_sku}** from "
        f"week {from_week} → week {to_week}."
    )
    st.rerun()


def _first_day_of_week(reference, target_week: int):
    """Return a pd.Timestamp for the first working day (Monday-ish) of the
    given week-of-month, anchored on the same month as ``reference``.

    Falls back to today's month if ``reference`` isn't a parseable date.
    """
    ref = pd.to_datetime(reference, errors="coerce")
    if pd.isna(ref):
        ref_dt = now_local()
    else:
        ref_dt = ref
    year = int(ref_dt.year)
    month = int(ref_dt.month)
    working = _working_days_in_month(year, month)
    # Pick the first working day whose week-of-month matches.
    for d in working:
        if week_of_month(d) == int(target_week):
            return pd.Timestamp(d)
    # Fallback — first working day of the month
    return pd.Timestamp(working[0]) if working else pd.Timestamp(ref_dt.date())


def _strip_one_sku(sku: str, valid_bases: set) -> "tuple[str, str]":
    """Return (cleaned_sku, suffix) for a single SKU.

    Strips a customer suffix from `sku` only when the resulting base
    exists in `valid_bases`. Mirrors `core/data_loader.collapse_customer_suffix`
    but takes an arbitrary base set so it works for FG *and* accessory
    catalogs.

    Returns the original SKU + empty suffix if no clean strip is possible.
    """
    s = str(sku or "").strip()
    if not s:
        return s, ""
    for sfx in CUSTOMER_SUFFIXES:
        if s.endswith(sfx):
            base = s[:-len(sfx)]
            if base in valid_bases:
                return base, sfx
    return s, ""


def _find_customer_suffixed_skus(
    schedule_df: pd.DataFrame, machine_skus: set, acc_skus: set,
) -> pd.DataFrame:
    """Detect FG / Accessory SKUs in the loaded schedule whose tail
    matches a CUSTOMER_SUFFIXES entry.

    Returns a small DataFrame with one row per unique (column, original-
    SKU) pair. Columns:
      Original  · Suffix  · Base  · Type (FG / Accessory)  · In catalog  · Rows
    """
    if schedule_df is None or schedule_df.empty:
        return pd.DataFrame()

    fg_col_candidates = ["FG SKU ID", "FG_RAW", "FG_BASE"]
    acc_col_candidates = ["FG ACCRY SKU ID", "ACC"]

    def _detect(col_candidates, valid_bases, type_label):
        out = []
        col = next((c for c in col_candidates if c in schedule_df.columns), None)
        if col is None:
            return out
        for sku, count in schedule_df[col].astype(str).str.strip().value_counts().items():
            base, sfx = _strip_one_sku(sku, valid_bases)
            if not sfx:
                continue  # no suffix detected → skip
            out.append({
                "Original": sku,
                "Suffix": sfx,
                "Base": base,
                "Type": type_label,
                "In catalog": "✓",  # _strip_one_sku only returns sfx when base is in valid_bases
                "Rows": int(count),
            })
        return out

    # Also catch the "suffix present but base NOT in catalog" case so the
    # planner sees it (and we leave those alone during the strip).
    def _detect_orphans(col_candidates, valid_bases, type_label):
        out = []
        col = next((c for c in col_candidates if c in schedule_df.columns), None)
        if col is None:
            return out
        for sku, count in schedule_df[col].astype(str).str.strip().value_counts().items():
            s = str(sku).strip()
            if not s:
                continue
            for sfx in CUSTOMER_SUFFIXES:
                if s.endswith(sfx):
                    base = s[:-len(sfx)]
                    if base not in valid_bases:
                        out.append({
                            "Original": s,
                            "Suffix": sfx,
                            "Base": base,
                            "Type": type_label,
                            "In catalog": "✗",
                            "Rows": int(count),
                        })
                    break  # only flag once per SKU
        return out

    rows = []
    rows.extend(_detect(fg_col_candidates, machine_skus, "FG"))
    rows.extend(_detect(acc_col_candidates, acc_skus, "Accessory"))
    rows.extend(_detect_orphans(fg_col_candidates, machine_skus, "FG"))
    rows.extend(_detect_orphans(acc_col_candidates, acc_skus, "Accessory"))
    if not rows:
        return pd.DataFrame(columns=["Original", "Suffix", "Base", "Type", "In catalog", "Rows"])
    return pd.DataFrame(rows).sort_values(
        by=["In catalog", "Type", "Rows"], ascending=[False, True, False],
    ).reset_index(drop=True)


def _strip_customer_suffixes_from_schedule(
    schedule_df: pd.DataFrame, machine_skus: set, acc_skus: set,
) -> "tuple[pd.DataFrame, int, int]":
    """Return a copy of schedule_df with customer suffixes stripped from
    FG and Accessory SKU columns. Also returns (n_fg_stripped, n_acc_stripped)
    row counts for the UI.

    Only strips when the base SKU exists in the matching catalog — orphan
    suffixed SKUs (no catalog base) are left untouched so the analysis
    doesn't silently lose them.
    """
    if schedule_df is None or schedule_df.empty:
        return schedule_df, 0, 0

    out = schedule_df.copy()
    n_fg = 0
    n_acc = 0

    # FG side — there can be several FG-shaped columns; strip whichever exist.
    for col in ("FG SKU ID", "FG_RAW", "FG_BASE"):
        if col not in out.columns:
            continue
        new_vals = []
        for v in out[col].astype(str).str.strip():
            base, sfx = _strip_one_sku(v, machine_skus)
            if sfx:
                new_vals.append(base)
                if col == "FG SKU ID":
                    n_fg += 1
            else:
                new_vals.append(v)
        out[col] = new_vals

    # Accessory side
    for col in ("FG ACCRY SKU ID", "ACC"):
        if col not in out.columns:
            continue
        new_vals = []
        for v in out[col].astype(str).str.strip():
            base, sfx = _strip_one_sku(v, acc_skus)
            if sfx:
                new_vals.append(base)
                if col == "FG ACCRY SKU ID":
                    n_acc += 1
            else:
                new_vals.append(v)
        out[col] = new_vals

    return out, n_fg, n_acc


def _render_suffix_cleanup_view(
    schedule_df: pd.DataFrame, machine_df, acc_df,
) -> None:
    """🧹 Customer suffix cleanup — sub-section UI.

    Surfaces suffixed FG / Accessory SKUs in the loaded schedule with
    their detected bases, plus one-click Strip / Download actions. Catalog
    rows are not touched — this is a *schedule display cleanup*, not a
    catalog editor.
    """
    st.subheader("🧹 Customer suffix cleanup")
    st.caption(
        "ERP exports often carry customer-decal suffixes "
        f"(**{', '.join(CUSTOMER_SUFFIXES)}**) on FG SKUs — e.g. "
        "`BOSS70-001HRC`, `BOSS25-006ES`. The labor analysis already "
        "strips these internally, but the suffixed text shows up in the "
        "Sequence editor, Calendar, and downloaded CSV. This tool lets "
        "you rewrite the loaded schedule to canonical base SKUs in one "
        "click. **Catalogs are not modified.**"
    )

    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then "
            "come back here to check for customer suffixes."
        )
        return

    # Build lookup sets from the catalogs.
    machine_skus = set(machine_df["SKU"].astype(str).str.strip()) if (
        machine_df is not None and "SKU" in machine_df.columns
    ) else set()
    acc_skus = set(acc_df["SKU"].astype(str).str.strip()) if (
        acc_df is not None and "SKU" in acc_df.columns
    ) else set()

    findings = _find_customer_suffixed_skus(schedule_df, machine_skus, acc_skus)
    _track_and_flush("suffix_cleanup_view", rows=int(len(findings)))

    if findings.empty:
        st.success(
            "✅ **Schedule already uses canonical SKUs.** No customer "
            "suffixes detected in either the FG or Accessory columns."
        )
        return

    # Show the detection panel.
    st.markdown("**Detected customer-suffixed SKUs:**")
    st.dataframe(findings, use_container_width=True, hide_index=True)

    in_catalog_count = int((findings["In catalog"] == "✓").sum())
    orphan_count = int((findings["In catalog"] == "✗").sum())
    if orphan_count:
        st.warning(
            f"⚠️ **{orphan_count} row(s) have a customer suffix but the "
            "base SKU isn't in the catalog** — those will be left alone "
            "by the Strip action so the analysis doesn't silently lose "
            "them. Add the base SKU to the catalog first if you want "
            "them stripped too."
        )

    cleaned, n_fg_preview, n_acc_preview = _strip_customer_suffixes_from_schedule(
        schedule_df, machine_skus, acc_skus,
    )

    c1, c2 = st.columns(2)
    if c1.button(
        f"🔄 Strip suffixes in loaded schedule "
        f"({n_fg_preview} FG, {n_acc_preview} accessory cells)",
        use_container_width=True, type="primary",
        disabled=(in_catalog_count == 0),
        help=(
            "Rewrites FG / Accessory SKU columns in your active schedule "
            "to their canonical base form. Pushes the cleaned schedule "
            "through the analysis pipeline so every tab refreshes on the "
            "next rerun. No GitHub commit."
        ),
    ):
        try:
            csv_text = cleaned.to_csv(index=False)
            st.session_state["_loaded_schedule_buffer"] = io.BytesIO(
                csv_text.encode("utf-8")
            )
            _track_and_flush(
                "suffix_cleanup_strip",
                n_fg=n_fg_preview, n_acc=n_acc_preview,
            )
            st.success(
                f"✅ Stripped suffixes from **{n_fg_preview} FG** + "
                f"**{n_acc_preview} accessory** cells — refreshing analysis…"
            )
            st.rerun()
        except Exception as e:
            st.error(f"❌ Strip failed: {e}")

    try:
        c2.download_button(
            "📥 Download cleaned schedule",
            cleaned.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_cleaned_{today_local_str()}.csv",
            mime="text/csv",
            use_container_width=True,
            on_click=lambda: _track_and_flush(
                "suffix_cleanup_download",
                n_fg=n_fg_preview, n_acc=n_acc_preview,
            ),
            help=(
                "Saves the cleaned schedule as a CSV. Re-upload, or save "
                "via the 📂 Save panel for the team."
            ),
        )
    except Exception:
        # Defensive — the on_click track-and-flush may have edge cases.
        c2.download_button(
            "📥 Download cleaned schedule",
            cleaned.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_cleaned_{today_local_str()}.csv",
            mime="text/csv",
            use_container_width=True,
        )


def _render_calendar_view(
    schedule_df: pd.DataFrame, machine_df, acc_df,
    time_window: str = "Whole month",
    plan_month: "tuple[int, int] | None" = None,
    plan_month_label: str = "",
) -> None:
    """📆 Calendar — week & day rollups of the schedule.

    Renders one expandable card per **Monday–Friday week** of the selected
    Plan month: total units, total labor, top FG SKUs. Inside each card: a
    daily breakdown table (units + labor per day) and a per-station labor-mix
    bar.

    Weeks are real calendar weeks (Monday-anchored) of the Plan month chosen
    in the sidebar — the first / last week may be partial. Only rows dated
    inside the Plan month appear here; rows in other months are flagged by a
    top-of-page warning in main().

    Designed to answer the planner's question *"what's being built each
    week / day?"* without forcing them to scroll a 156-row table.

    Calendar **always** shows the entire month — even when the sidebar's
    Time window is set to "This week" or "Remaining (from today)" (which
    filter the *analysis* tabs). A small note is shown when the planner is
    in a filtered window so they understand the relationship.
    """
    from core.constants import STATION_KEY_TO_DISPLAY

    _heading = f"📆 Calendar — {plan_month_label}" if plan_month_label else "📆 Calendar"
    st.subheader(_heading)
    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then come "
            "back here for the week / day rollup."
        )
        return

    if time_window != "Whole month":
        st.caption(
            f"ℹ️ Showing the **full month** below. Your sidebar Time window "
            f"is set to **{time_window}** — that filter only affects the "
            "analysis tabs (Overview / Capacity / etc.), not this Calendar view."
        )

    if "PRODUCTION DAY" not in schedule_df.columns:
        st.info(
            "📅 This view needs `PRODUCTION DAY` data. Your CSV doesn't have "
            "one, and auto-spread didn't fill it in (uncommon). Add a "
            "PRODUCTION DAY column to your schedule and re-upload."
        )
        return

    # Normalize: real datetime, drop rows the loader couldn't date.
    work = schedule_df.copy()
    work["PRODUCTION DAY"] = pd.to_datetime(work["PRODUCTION DAY"], errors="coerce")
    work = work.dropna(subset=["PRODUCTION DAY"])
    if work.empty:
        st.info("No rows have a valid PRODUCTION DAY value.")
        return

    # Restrict to the selected Plan month — the picker is authoritative, so the
    # Calendar only divides / shows that month. (Out-of-month rows get a
    # top-of-page warning in main(); they're intentionally not shown here.)
    if plan_month is not None:
        _py, _pm = plan_month
        work = work[
            (work["PRODUCTION DAY"].dt.year == _py)
            & (work["PRODUCTION DAY"].dt.month == _pm)
        ].copy()
        if work.empty:
            st.info(
                f"No units are dated in **{plan_month_label}**. Either pick a "
                "different **Plan month** in the sidebar, or add dates in that "
                "month to your schedule."
            )
            return

    # Recompute WEEK_OF_MONTH on the filtered frame so it matches the
    # Monday-anchored scheme used for the week cards below.
    work["WEEK_OF_MONTH"] = week_of_month_series(work["PRODUCTION DAY"])

    # Map each Monday-anchored week of the Plan month → its Mon–Fri working
    # days (clamped to the month) so every card shows a real calendar span,
    # e.g. "Jun 1 – Jun 5", even for partial first / last weeks.
    _ym = plan_month if plan_month is not None else (
        int(work["PRODUCTION DAY"].dt.year.mode()[0]),
        int(work["PRODUCTION DAY"].dt.month.mode()[0]),
    )
    _week_days: dict[int, list] = {}
    for _d in _working_days_in_month(_ym[0], _ym[1]):
        _week_days.setdefault(week_of_month(_d), []).append(_d)

    # Per-row labor breakdown (station-level p-min × qty). Cache once so
    # we can sum it both by week and by day cheaply.
    breakdowns: list[dict] = []
    totals: list[float] = []
    for idx in work.index:
        row = work.loc[idx]
        fg = row.get("FG_BASE") or row.get("FG SKU ID")
        acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
        try:
            qty = float(row.get("BUILD QTY", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            labor = compute_unit_labor(fg, acc, machine_df, acc_df)
        except Exception:
            labor = None
        if labor is None:
            br = {s: 0.0 for s in STATION_KEYS}
            total = max(qty, 1.0) * 100.0  # crude fallback (unknown SKU)
        else:
            br = {s: float(labor.get(s, 0) or 0) * qty for s in STATION_KEYS}
            total = sum(br.values())
        breakdowns.append(br)
        totals.append(total)
    work["_labor"] = totals
    # Attach per-station columns prefixed with __st_ so we can sum them.
    for s in STATION_KEYS:
        work[f"__st_{s}"] = [b.get(s, 0.0) for b in breakdowns]

    # Glance metrics at the top.
    total_units = int(work["BUILD QTY"].sum())
    total_labor = float(work["_labor"].sum())
    n_weeks = int(work["WEEK_OF_MONTH"].nunique())
    c1, c2, c3 = st.columns(3)
    c1.metric("Units in plan", f"{total_units:,}")
    c2.metric("Total labor", f"{int(total_labor):,} p-min")
    c3.metric("Weeks", n_weeks)
    st.caption(
        "Each week below is an expander. Open one to see the day-by-day "
        "breakdown and the per-station labor mix."
    )
    st.markdown("---")

    # Iterate weeks in order.
    weeks = sorted(int(w) for w in work["WEEK_OF_MONTH"].dropna().unique())
    for week_num in weeks:
        wk_df = work[work["WEEK_OF_MONTH"] == week_num]
        if wk_df.empty:
            continue
        wk_units = int(wk_df["BUILD QTY"].sum())
        wk_labor = float(wk_df["_labor"].sum())
        # Day range — the week's Monday–Friday span in the month (clamped),
        # so the label reads as a real calendar week regardless of which days
        # actually have units. Falls back to the days used if the week isn't
        # in the month map (defensive).
        _wk_span = _week_days.get(week_num) or sorted(
            wk_df["PRODUCTION DAY"].dt.date.unique()
        )
        date_label = (
            f"{_wk_span[0].strftime('%b')} {_wk_span[0].day} – "
            f"{_wk_span[-1].strftime('%b')} {_wk_span[-1].day}"
            if len(_wk_span) > 1 else
            (f"{_wk_span[0].strftime('%b')} {_wk_span[0].day}" if _wk_span else "—")
        )
        # Top 3 FG SKUs by qty
        top_skus = (
            wk_df.groupby(wk_df["FG SKU ID"].astype(str))["BUILD QTY"]
            .sum()
            .sort_values(ascending=False)
            .head(3)
        )
        top_label = " · ".join(
            f"{sku} ×{int(q)}" for sku, q in top_skus.items()
        ) or "—"

        with st.expander(
            f"📆 **Week {week_num}** ({date_label}) — "
            f"{wk_units} units · {int(wk_labor):,} p-min  ·  Top: {top_label}",
            expanded=False,
        ):
            # ---- Daily rollup ----
            st.markdown("**Daily breakdown**")
            daily = (
                wk_df.assign(_d=wk_df["PRODUCTION DAY"].dt.date)
                .groupby("_d")
                .agg(
                    units=("BUILD QTY", "sum"),
                    labor=("_labor", "sum"),
                )
                .reset_index()
                .rename(columns={"_d": "Day"})
            )
            daily["Day"] = daily["Day"].apply(
                lambda d: f"{d.strftime('%a %b')} {d.day}"
            )
            daily["units"] = daily["units"].astype(int)
            daily["labor"] = daily["labor"].round(0).astype(int)
            daily = daily.rename(columns={
                "units": "Units",
                "labor": "Labor (p-min)",
            })
            st.dataframe(
                daily, use_container_width=True, hide_index=True,
            )

            # ---- Per-station labor mix ----
            st.markdown("**Per-station labor mix this week**")
            station_totals = {
                STATION_KEY_TO_DISPLAY.get(s, s): float(wk_df[f"__st_{s}"].sum())
                for s in STATION_KEYS
            }
            # Drop zero-stations to keep the chart focused.
            mix_df = pd.DataFrame(
                [
                    {"Station": st_disp, "Labor (p-min)": v}
                    for st_disp, v in station_totals.items()
                    if v > 0
                ]
            ).sort_values("Labor (p-min)", ascending=True)
            if mix_df.empty:
                st.caption("_No labor recorded for this week's units._")
            else:
                _bar = px.bar(
                    mix_df, x="Labor (p-min)", y="Station",
                    orientation="h", text_auto=",.0f",
                )
                _bar.update_layout(
                    height=min(360, 30 * len(mix_df) + 80),
                    margin=dict(l=4, r=4, t=4, b=4),
                    yaxis_title=None,
                )
                _bar.update_traces(marker_color="#4F46E5")
                # Unique key per week so Streamlit doesn't collide IDs
                # when multiple weeks' bars have similar shapes.
                st.plotly_chart(
                    _bar, use_container_width=True,
                    key=f"calendar_week_{week_num}_mix",
                )

            # ---- SKU detail table (collapsed) ----
            with st.expander(f"📋 Units in week {week_num} ({wk_units})", expanded=False):
                detail = wk_df.copy()
                detail["Day"] = detail["PRODUCTION DAY"].apply(
                    lambda d: f"{d.strftime('%a %b')} {d.day}"
                )
                cols = [
                    c for c in ["Day", "FG SKU ID", "FG ACCRY SKU ID",
                                "BUILD QTY", "CUSTOMER NAME", "LOCATION"]
                    if c in detail.columns
                ]
                renames = {
                    "FG SKU ID": "FG SKU",
                    "FG ACCRY SKU ID": "Accessory",
                    "BUILD QTY": "Qty",
                    "CUSTOMER NAME": "Customer",
                    "LOCATION": "Location",
                }
                st.dataframe(
                    detail[cols].rename(columns=renames),
                    use_container_width=True,
                    hide_index=True,
                )


# =============================================================
# SKU browser — supports the manual-entry "Browse catalog" panel
# =============================================================
def _render_scenarios_panel(location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander for saving / loading named build-plan scenarios for the
    current facility. Persists to data/scenarios.json on GitHub."""
    # Always reload the latest scenarios from the cache (keyed on file mtime).
    scenarios = _load_scenarios_dict(_csv_mtime("scenarios.json")).get(location, {})
    scenario_names = sorted(scenarios.keys()) if isinstance(scenarios, dict) else []

    with st.sidebar.expander("📂 Save / load scenarios", expanded=False):
        # Replay any one-shot success / info / error message stashed before rerun
        toast_key = f"_scenario_toast_{location}"
        toast = st.session_state.pop(toast_key, None)
        if toast:
            level, msg = toast
            if level == "success":
                st.success(msg)
            elif level == "info":
                st.info(msg)
            else:
                st.error(msg)

        st.caption(
            f"Scenarios for **{location}** only. "
            "Saved to `data/scenarios.json` on GitHub — shared across all users."
        )

        # ------------------------------------------------------------------
        # SAVE
        # ------------------------------------------------------------------
        st.markdown("**Save current as**")
        save_name = st.text_input(
            "Scenario name",
            key=f"scenario_save_name_{location}",
            placeholder="e.g. May 2026 push",
            label_visibility="collapsed",
        )
        if st.button(
            "💾 Save scenario",
            key=f"scenario_save_btn_{location}",
            use_container_width=True,
        ):
            _scenario_save(location, save_name, seed_key, scenario_names)

        # ------------------------------------------------------------------
        # LOAD / DELETE
        # ------------------------------------------------------------------
        st.markdown("---")
        st.markdown("**Load a saved scenario**")
        if not scenario_names:
            st.caption(f"_No saved scenarios for {location} yet._")
            return

        chosen = st.selectbox(
            "Scenario",
            options=scenario_names,
            key=f"scenario_load_pick_{location}",
            label_visibility="collapsed",
        )
        # Show metadata for the chosen scenario
        meta = scenarios.get(chosen, {}) if isinstance(scenarios, dict) else {}
        n_entries = len(meta.get("entries", []) or [])
        saved_at = meta.get("saved_at", "")
        st.caption(f"📋 {n_entries} row(s)  ·  saved {saved_at}")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("📥 Load", key=f"scenario_load_btn_{location}",
                         use_container_width=True):
                _scenario_load(location, chosen, seed_key, rev_key)
        with c2:
            if st.button("🗑 Delete", key=f"scenario_del_btn_{location}",
                         use_container_width=True):
                _scenario_delete(location, chosen)


def _scenario_save(location: str, name: str, seed_key: str, existing_names: list) -> None:
    """Handler for the Save button — validates and pushes the current
    manual-entry rows as a named scenario."""
    name = (name or "").strip()
    if not name:
        st.warning("Please enter a scenario name before saving.")
        return

    # Build the entries DataFrame from the latest manual-editor state. Prefer
    # the current data_editor key (so any in-flight edits are captured);
    # fall back to the seed if the editor hasn't rendered yet.
    rev = st.session_state.get(f"manual_rev_{location}", 0)
    editor_key = f"manual_entries_{location}_v{rev}"
    df = st.session_state.get(editor_key)
    if not isinstance(df, pd.DataFrame):
        df = st.session_state.get(seed_key, pd.DataFrame(
            columns=["FG SKU", "Accessory SKU", "Qty"]
        ))

    # Check empty after stripping blank rows — let save handler do the cleaning
    non_empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not non_empty.empty:
        non_empty["__fg"] = non_empty.get("FG SKU", "").astype(str).str.strip()
        non_empty["__qty"] = pd.to_numeric(
            non_empty.get("Qty", 0), errors="coerce",
        ).fillna(0).astype(int)
        non_empty = non_empty[(non_empty["__fg"] != "") & (non_empty["__qty"] > 0)]
    if non_empty.empty:
        st.warning(
            "The manual-entry table is empty. Fill in at least one row (FG SKU + Qty > 0) "
            "before saving the scenario."
        )
        return

    # Single per-location confirm key — stores the name the user was warned
    # about on the previous click. We only honor it if the name STILL matches
    # the current attempt, so a stale flag for an old name (left over after
    # the user typed a new name and saved cleanly) can't fire an unconfirmed
    # overwrite of a different scenario later.
    confirm_key = f"_scenario_overwrite_pending_{location}"
    if name in existing_names:
        if st.session_state.get(confirm_key) != name:
            st.session_state[confirm_key] = name
            st.warning(
                f"A scenario named **{name}** already exists for {location}. "
                "Click **💾 Save scenario** again to overwrite it."
            )
            return
        # Second click on the SAME name — clear the flag and fall through.
        st.session_state.pop(confirm_key, None)
    else:
        # Saving a different (non-existing) name — drop any stale warning state
        # so a later attempt at an existing name re-triggers the confirmation.
        st.session_state.pop(confirm_key, None)

    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    try:
        with st.spinner(f"Saving scenario '{name}'..."):
            response = save_scenario_to_github(location, name, df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("scenario_save", facility=location, name=name)
        # Belt and braces — clear the confirm flag after a successful save.
        st.session_state.pop(confirm_key, None)
        # Stash a one-shot success message that survives the upcoming rerun
        st.session_state[f"_scenario_toast_{location}"] = (
            "success",
            f"✅ Saved scenario **{name}** for {location}"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Save failed: {e}")


def _scenario_load(location: str, name: str, seed_key: str, rev_key: str) -> None:
    """Handler for the Load button — replaces the manual-editor's seed with
    the saved scenario's rows and forces the editor to re-render."""
    block = get_scenario(location, name) or {}
    rows = block.get("entries", []) or []
    if not rows:
        st.warning(f"Scenario '{name}' has no entries.")
        return
    seed = pd.DataFrame(rows, columns=["FG SKU", "Accessory SKU", "Qty"])
    # Coerce Qty to int (json round-trip can produce floats)
    seed["Qty"] = pd.to_numeric(seed["Qty"], errors="coerce").fillna(0).astype(int)
    st.session_state[seed_key] = seed
    st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
    # Stash the scenario name so manual-mode auto-spread can parse a
    # target month from it (e.g. "Hen 2026 June" → June 2026 instead of
    # defaulting to today's month).
    st.session_state[f"_last_loaded_scenario_name_{location}"] = name
    # Don't force-flush here — load is a read; let buffer accumulate.
    track_event("scenario_load", facility=location, name=name)
    st.success(f"Loaded scenario **{name}** — {len(seed)} row(s).")
    st.rerun()


def _scenario_delete(location: str, name: str) -> None:
    """Handler for the Delete button."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner(f"Deleting scenario '{name}'..."):
            delete_scenario_on_github(location, name, token)
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("scenario_delete", facility=location, name=name)
        st.session_state[f"_scenario_toast_{location}"] = (
            "success", f"🗑 Deleted scenario **{name}** for {location}.",
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Delete failed: {e}")


# =============================================================
# Uploaded-schedule save/load/delete — sidebar expander.
# Mirrors the scenarios pattern (_scenario_save/load/delete) but stores
# the raw CSV bytes in data/uploaded_schedules.json so a teammate can
# reload the exact same schedule later. Per-facility keyed.
# =============================================================
def _render_uploaded_schedules_panel(location: str, uploaded_file) -> None:
    """Sidebar expander for saving / loading named uploaded schedules.

    ``uploaded_file`` is the live ``st.file_uploader`` widget value (or None).
    When the planner clicks Save, we serialize those bytes to text and push
    to GitHub. When they click Load, we stash a BytesIO in session_state
    that ``_load_schedule_df`` consumes one-shot on the next rerun.
    """
    all_schedules = _load_uploaded_schedules_dict(_csv_mtime("uploaded_schedules.json"))
    by_loc = all_schedules.get(location, {}) if isinstance(all_schedules, dict) else {}
    schedule_names = sorted(by_loc.keys()) if isinstance(by_loc, dict) else []

    with st.sidebar.expander("📂 Save / load uploaded schedules", expanded=False):
        # Replay any one-shot toast stashed before rerun
        toast_key = f"_uploaded_schedule_toast_{location}"
        toast = st.session_state.pop(toast_key, None)
        if toast:
            level, msg = toast
            if level == "success":
                st.success(msg)
            elif level == "info":
                st.info(msg)
            else:
                st.error(msg)

        st.caption(
            f"Saved schedules for **{location}** only. "
            "Pushed to `data/uploaded_schedules.json` on GitHub — shared "
            "with the whole team."
        )

        # ------------------------------------------------------------------
        # SAVE — uses the live uploader bytes if present, otherwise the
        # bundled sample (so a planner can save the "current view" they're
        # working with without re-uploading).
        # ------------------------------------------------------------------
        st.markdown("**Save current upload as**")
        save_name = st.text_input(
            "Schedule name",
            key=f"uploaded_schedule_save_name_{location}",
            placeholder="e.g. May 2026 production plan",
            label_visibility="collapsed",
        )
        if st.button(
            "💾 Save schedule",
            key=f"uploaded_schedule_save_btn_{location}",
            use_container_width=True,
            disabled=(uploaded_file is None),
            help=(
                "Saves the file you uploaded above to GitHub under this "
                "facility, so teammates can reload it. Upload a file first."
                if uploaded_file is None else
                "Pushes your current uploaded CSV to GitHub. Teammates can "
                "reload it from the dropdown below."
            ),
        ):
            _uploaded_schedule_save(location, save_name, uploaded_file, schedule_names)

        # ------------------------------------------------------------------
        # LOAD + DELETE
        # ------------------------------------------------------------------
        st.markdown("**Load saved**")
        if not schedule_names:
            st.caption("_No saved schedules for this facility yet._")
        else:
            chosen = st.selectbox(
                "Saved schedule",
                schedule_names,
                key=f"uploaded_schedule_load_pick_{location}",
                label_visibility="collapsed",
            )
            block = by_loc.get(chosen) or {}
            saved_at = block.get("saved_at", "?")
            rows = block.get("rows", "?")
            st.caption(f"Last saved: **{saved_at}** · **{rows}** rows")
            c1, c2 = st.columns([3, 1])
            if c1.button(
                "📥 Load", key=f"uploaded_schedule_load_btn_{location}",
                use_container_width=True,
            ):
                _uploaded_schedule_load(location, chosen)
            if c2.button(
                "🗑 Delete", key=f"uploaded_schedule_del_btn_{location}",
                use_container_width=True,
            ):
                _uploaded_schedule_delete(location, chosen)


def _uploaded_schedule_save(
    location: str, name: str, uploaded_file, existing_names: list,
) -> None:
    """Save the planner's currently-uploaded CSV under ``name`` to GitHub.

    Mirrors ``_scenario_save`` — same overwrite-confirmation pattern (per-
    location key carrying the name we warned about, cleared on save) so a
    stale flag can't fire an unconfirmed overwrite later.
    """
    name = (name or "").strip()
    if not name:
        st.warning("Please enter a schedule name before saving.")
        return
    if uploaded_file is None:
        st.warning("Upload a CSV first, then save it.")
        return

    confirm_key = f"_uploaded_schedule_overwrite_pending_{location}"
    if name in existing_names:
        if st.session_state.get(confirm_key) != name:
            st.session_state[confirm_key] = name
            st.warning(
                f"A schedule named **{name}** already exists for {location}. "
                "Click **💾 Save schedule** again to overwrite it."
            )
            return
        st.session_state.pop(confirm_key, None)
    else:
        st.session_state.pop(confirm_key, None)

    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    # Decode the upload bytes to text. The loader handles latin-1 / UTF-8 /
    # UTF-8-with-BOM transparently; here we just need *some* text encoding
    # we can re-parse. Try utf-8 first, fall back to latin-1.
    try:
        raw = uploaded_file.getvalue()
        try:
            csv_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = raw.decode("latin-1")
    except Exception as e:
        st.error(f"❌ Couldn't read the uploaded file: {e}")
        return

    try:
        with st.spinner(f"Saving schedule '{name}' for {location}..."):
            response = save_uploaded_schedule_to_github(location, name, csv_text, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush(
            "schedule_save", facility=location, name=name,
            rows=len(csv_text.splitlines()) - 1,
        )
        st.session_state.pop(confirm_key, None)
        st.session_state[f"_uploaded_schedule_toast_{location}"] = (
            "success",
            f"✅ Saved schedule **{name}** for {location}"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Save failed: {e}")


def _uploaded_schedule_load(location: str, name: str) -> None:
    """Load a saved schedule by stashing its CSV bytes into session_state.

    ``_load_schedule_df`` picks the buffer up on the next rerun and feeds it
    to ``load_schedule`` as if the planner had just uploaded the file. The
    buffer is cleared once consumed, so a fresh upload still wins on the
    rerun after that.
    """
    block = get_uploaded_schedule(location, name) or {}
    csv_text = block.get("csv_content", "")
    if not csv_text:
        st.warning(f"Saved schedule '{name}' is empty.")
        return
    st.session_state["_loaded_schedule_buffer"] = io.BytesIO(csv_text.encode("utf-8"))
    _track_and_flush("schedule_load_saved", facility=location, name=name)
    st.success(f"Loaded saved schedule **{name}** for {location}.")
    st.rerun()


def _uploaded_schedule_delete(location: str, name: str) -> None:
    """Handler for the Delete button."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner(f"Deleting saved schedule '{name}'..."):
            delete_uploaded_schedule_on_github(location, name, token)
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("schedule_delete", facility=location, name=name)
        st.session_state[f"_uploaded_schedule_toast_{location}"] = (
            "success", f"🗑 Deleted saved schedule **{name}** for {location}.",
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Delete failed: {e}")


def _render_sku_browser(machine_df, acc_df, location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander that lets the user filter the machine catalog by
    family + class and add a chosen FG SKU + accessory + qty into the
    manual entries table (via session_state[seed_key])."""
    # Build a working frame with classification columns we can filter on.
    # reset_index(drop=True) avoids the SKU index/column ambiguity that
    # bites sort_values later.
    work = machine_df.reset_index(drop=True).copy()
    work["__family"] = work["SKU"].astype(str).apply(_machine_family)
    work["__class"] = work.apply(
        lambda r: _machine_class(r["SKU"], r.get("Description", "")), axis=1,
    )

    # Helpers used by both directions
    def _fg_label_for_row(row):
        bat = int(row.get("Bat", 0) or 0)
        cls = row.get("__class", "")
        bat_part = f" · {bat} batt" if bat > 0 else ""
        placeholder = " ⚠" if cls == "⚠ Placeholder" else ""
        return f"{row['SKU']} — {cls}{bat_part}{placeholder}"

    def _acc_label(sku: str) -> str:
        if sku == "(none)":
            return "(no accessory)"
        placeholder = " ⚠ family placeholder" if "999" in sku or "XXX" in sku.upper() else ""
        return f"{sku}{placeholder}"

    def _commit_add(fg_choice: str, acc_choice: str, qty: int) -> None:
        """Append the chosen row to the manual entries DataFrame and bump the rev."""
        editor_key = f"manual_entries_{location}_v{st.session_state.get(rev_key, 0)}"
        latest = st.session_state.get(editor_key)
        if isinstance(latest, pd.DataFrame):
            current = latest.copy()
        else:
            current = st.session_state.get(
                seed_key, pd.DataFrame(columns=["FG SKU", "Accessory SKU", "Qty"]),
            ).copy()
        current = pd.concat(
            [current, pd.DataFrame([{
                "FG SKU": fg_choice,
                "Accessory SKU": "" if acc_choice == "(none)" else acc_choice,
                "Qty": int(qty),
            }])],
            ignore_index=True,
        )
        st.session_state[seed_key] = current
        st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
        st.success(f"Added {fg_choice} × {int(qty)} to manual entries.")
        st.rerun()

    with st.sidebar.expander("📚 Browse catalog & add SKU", expanded=False):
        direction = st.radio(
            "Start from",
            options=["FG SKU first", "Accessory SKU first"],
            horizontal=True,
            help=(
                "**FG SKU first** — pick a finished good, then match accessory.  \n"
                "**Accessory SKU first** — pick an accessory, then match a finished good."
            ),
            key=f"browse_direction_{location}",
        )

        if direction == "FG SKU first":
            _render_fg_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)
        else:
            _render_acc_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)


def _render_fg_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """FG-first browse flow: Family → Class → FG SKU → Accessory → Qty."""
    family_counts = work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f in family_counts.index]
    if "OTHER" in family_counts.index:
        ordered_families.append("OTHER")
    family_choice = st.selectbox(
        "Family",
        options=ordered_families,
        format_func=lambda f: f"{f} ({family_counts.get(f, 0)})",
        key=f"browse_family_{location}",
    )

    in_family = work[work["__family"] == family_choice]
    class_counts = in_family["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in class_counts.index]
    for c in class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "Class",
        options=["All classes"] + ordered_classes,
        key=f"browse_class_{location}",
    )

    if class_choice == "All classes":
        sku_rows = in_family
    else:
        sku_rows = in_family[in_family["__class"] == class_choice]
    sku_rows = sku_rows.sort_values(by="SKU")
    sku_options = sku_rows["SKU"].astype(str).tolist()
    if not sku_options:
        st.info("No SKUs match this family + class combination.")
        return

    fg_choice = st.selectbox(
        "FG SKU",
        options=sku_options,
        format_func=lambda s: fg_label_for_row(sku_rows[sku_rows["SKU"] == s].iloc[0]),
        key=f"browse_fg_{location}",
    )

    acc_options = ["(none)"]
    if acc_df is not None and not acc_df.empty:
        acc_work = acc_df.reset_index(drop=True).copy()
        acc_skus = acc_work["SKU"].astype(str)
        acc_mask = acc_skus.apply(_accessory_family_hint).str.upper() == family_choice.upper()
        acc_subset = acc_work[acc_mask].sort_values(by="SKU")
        acc_options.extend(acc_subset["SKU"].astype(str).tolist())

    acc_choice = st.selectbox(
        "Accessory",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_{location}",
    )
    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_qty_{location}",
    )
    if st.button("➕ Add to manual entries", use_container_width=True,
                 key=f"browse_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


def _render_acc_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """Accessory-first browse flow: Family → Accessory SKU → matching FG SKUs → Qty.

    Useful when the planner knows which accessory kit a customer ordered and
    wants the system to suggest a compatible finished-good unit.
    """
    if acc_df is None or acc_df.empty:
        st.info("No accessory catalog loaded.")
        return

    acc_work = acc_df.reset_index(drop=True).copy()
    acc_work["__family"] = acc_work["SKU"].astype(str).apply(_accessory_family_hint).str.upper()

    # Family selector — uses accessory-side family counts
    fam_counts = acc_work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f.upper() in fam_counts.index]
    if not ordered_families:
        st.info("No accessories grouped by known families.")
        return
    family_choice = st.selectbox(
        "Family (accessories)",
        options=ordered_families,
        format_func=lambda f: f"{f} ({int(fam_counts.get(f.upper(), 0))})",
        key=f"browse_acc_family_{location}",
    )

    # Accessory SKU dropdown (filtered to chosen family)
    family_up = family_choice.upper()
    in_family_acc = acc_work[acc_work["__family"] == family_up].sort_values(by="SKU")
    acc_options = in_family_acc["SKU"].astype(str).tolist()
    if not acc_options:
        st.info("No accessories in this family.")
        return
    acc_choice = st.selectbox(
        "Accessory SKU",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_pick_{location}",
    )

    # Show description hint for the chosen accessory
    chosen_row = in_family_acc[in_family_acc["SKU"] == acc_choice]
    if not chosen_row.empty:
        desc = str(chosen_row.iloc[0].get("Description", "") or "").strip()
        if desc:
            st.caption(f"📋 {desc}")

    # Suggested FG SKUs — machines in the SAME family as the accessory
    matching_fg = work[work["__family"] == family_choice]
    # Allow optional class filter for the FG suggestion too
    fg_class_counts = matching_fg["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in fg_class_counts.index]
    for c in fg_class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "FG class filter",
        options=["All classes"] + ordered_classes,
        key=f"browse_acc_fgclass_{location}",
    )
    if class_choice != "All classes":
        matching_fg = matching_fg[matching_fg["__class"] == class_choice]
    matching_fg = matching_fg.sort_values(by="SKU")
    fg_options = matching_fg["SKU"].astype(str).tolist()

    if not fg_options:
        st.warning(
            f"No machine SKUs match the **{family_choice}** family for accessory "
            f"`{acc_choice}`. Add the accessory by itself or pick a different "
            "accessory family."
        )
        return

    fg_choice = st.selectbox(
        "Suggested FG SKU",
        options=fg_options,
        format_func=lambda s: fg_label_for_row(matching_fg[matching_fg["SKU"] == s].iloc[0]),
        key=f"browse_acc_fg_{location}",
    )

    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_acc_qty_{location}",
    )
    if st.button("➕ Add to manual entries", use_container_width=True,
                 key=f"browse_acc_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


# =============================================================
# Sidebar — global inputs
# =============================================================
def _render_admin_dashboard() -> None:
    """Hidden analytics view — KPIs from data/usage_log.jsonl.

    Rendered inside an ``⚙️ Admin`` expander at the bottom of the sidebar.
    No password gate: the expander label is intentionally generic so regular
    users don't notice it; only read-only charts/tables are shown, so there's
    nothing destructive even if a curious user opens it.
    """
    # Pull the on-disk log + any in-session un-flushed events
    try:
        mtime = _USAGE_LOG_PATH.stat().st_mtime if _USAGE_LOG_PATH.exists() else 0.0
    except Exception:
        mtime = 0.0
    events: list = list(_load_usage_log_cached(mtime) or [])
    events.extend(_usage_buffer_snapshot())

    if not events:
        st.caption("No usage events yet. Once people start using the tool, you'll see KPIs here.")
        if st.button("🔄 Refresh", key="_admin_refresh_empty"):
            _refresh_usage_from_github(_usage_token())
            try:
                st.cache_data.clear()
            except Exception:
                pass
            st.rerun()
        return

    # ---- Build dataframe ----------------------------------------------
    df = pd.DataFrame(events)
    if "ts" not in df.columns:
        st.caption("Usage log is malformed — no `ts` column.")
        return
    # Parse timestamps with utc=True so old naive entries (which were UTC
    # wall-clock from Streamlit Cloud) and new tz-aware LA entries land in
    # the same column. Convert to Las Vegas so the rendered "When" matches
    # the planner's clock regardless of which generation wrote the row.
    df["ts"] = (
        pd.to_datetime(df["ts"], errors="coerce", utc=True)
          .dt.tz_convert("America/Los_Angeles")
    )
    df = df.dropna(subset=["ts"]).copy()
    if df.empty:
        st.caption("No parseable events yet.")
        return
    df["date"] = df["ts"].dt.date
    df["type"] = df.get("type", "").astype(str)
    df["facility"] = df.get("facility", "").astype(str) if "facility" in df.columns else ""
    df["uid"] = df.get("uid", "").astype(str) if "uid" in df.columns else ""
    df["file"] = df.get("file", "").astype(str) if "file" in df.columns else ""

    # Admin time buckets in Las Vegas wall-clock so they match the timestamps
    # the rest of the app writes (Streamlit Cloud's servers run UTC).
    today = now_local().date()
    week_ago = today - pd.Timedelta(days=6)
    fortnight_ago = today - pd.Timedelta(days=13)

    sessions = df[df["type"] == "session_start"].copy()
    sessions_today = int(((sessions["date"] == today)).sum())
    sessions_week = int(((sessions["date"] >= week_ago)).sum())
    sessions_all = int(len(sessions))

    # ---- KPIs (compact metric row) ------------------------------------
    st.markdown("**📈 Sessions**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Today", sessions_today)
    c2.metric("This week", sessions_week)
    c3.metric("All-time", sessions_all)

    # Last 14 days line chart
    if not sessions.empty:
        daily = (
            sessions.assign(d=sessions["ts"].dt.date)
                    .groupby("d").size().reset_index(name="sessions")
        )
        # Re-index over a full 14-day window so empty days appear as zero
        full_range = pd.date_range(end=today, periods=14, freq="D").date
        full_df = pd.DataFrame({"d": full_range})
        merged = full_df.merge(daily, on="d", how="left").fillna({"sessions": 0})
        fig = px.line(
            merged, x="d", y="sessions", markers=True,
            labels={"d": "", "sessions": "Sessions"},
        )
        fig.update_layout(
            height=160, margin=dict(l=4, r=4, t=8, b=4),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("_No session events yet._")

    # ---- Facility breakdown -------------------------------------------
    st.markdown("**🏭 Sessions by facility**")
    if sessions.empty or sessions["facility"].eq("").all():
        st.caption("_No facility data yet._")
    else:
        fac = (
            sessions[sessions["facility"] != ""]
            .groupby("facility").size().reset_index(name="sessions")
            .sort_values("sessions", ascending=False)
        )
        cols = st.columns(min(len(fac), 3) or 1)
        for i, row in enumerate(fac.itertuples(index=False)):
            cols[i % len(cols)].metric(row.facility, int(row.sessions))

    # ---- Saves (last 14 days) -----------------------------------------
    st.markdown("**💾 Saves (last 14 days)**")
    save_types = {
        "catalog_save", "catalog_add_sku", "catalog_add_placeholder",
        "recon_apply", "scenario_save", "scenario_delete",
        "flow_save", "flow_reset", "simple_save", "crew_save",
    }
    saves = df[(df["type"].isin(save_types)) & (df["date"] >= fortnight_ago)].copy()
    if saves.empty:
        st.caption("_No saves in the last 14 days._")
    else:
        # Group by file when present, else by event type
        saves["bucket"] = saves["file"].where(saves["file"] != "", saves["type"])
        agg = (
            saves.groupby(["bucket", "type"]).size()
            .reset_index(name="count").sort_values("count", ascending=False)
        )
        agg.columns = ["File / target", "Event", "Count"]
        st.dataframe(agg, use_container_width=True, hide_index=True)

    # ---- Top actions ---------------------------------------------------
    st.markdown("**🎯 Top actions (last 14 days)**")
    recent = df[df["date"] >= fortnight_ago].copy()
    if recent.empty:
        st.caption("_No events in the last 14 days._")
    else:
        top = (
            recent.groupby("type").size().reset_index(name="count")
                  .sort_values("count", ascending=False).head(10)
        )
        top.columns = ["Event type", "Count"]
        st.dataframe(top, use_container_width=True, hide_index=True)

    # ---- Recent activity table ----------------------------------------
    st.markdown("**📋 Recent activity (last 20 events)**")
    last20 = df.sort_values("ts", ascending=False).head(20).copy()
    last20["When"] = last20["ts"].dt.strftime("%m-%d %H:%M")
    last20["UID"] = last20["uid"].str[-6:]
    cols_to_show = ["When", "UID", "type", "facility", "file"]
    cols_to_show = [c for c in cols_to_show if c in last20.columns]
    st.dataframe(
        last20[cols_to_show].rename(
            columns={"type": "Event", "facility": "Facility", "file": "File"}
        ),
        use_container_width=True, hide_index=True,
    )

    # ---- Refresh button ------------------------------------------------
    st.markdown("---")
    cc1, cc2 = st.columns([3, 2])
    cc1.caption(
        f"Buffered (un-flushed) this session: {len(_usage_buffer_snapshot())} event(s)"
    )
    if cc2.button("🔄 Refresh", key="_admin_refresh", use_container_width=True):
        _refresh_usage_from_github(_usage_token())
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.rerun()


def render_sidebar() -> dict:
    st.sidebar.title("🏭 Labor Planning")
    st.sidebar.caption("Set up your scenario, then review tabs on the right.")
    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 1 — Facility (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 1 · Pick a facility")
    location = st.sidebar.selectbox(
        "Facility",
        LOCATIONS,
        index=0,
        label_visibility="collapsed",
        help="Each facility has its own saved headcount and station setup.",
    )

    # ----------------------------------------------------------------
    # STEP 2 — Schedule (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 2 · Tell us what to build")
    schedule_mode = st.sidebar.radio(
        "How would you like to enter the build plan?",
        ["📤 Upload schedule file", "✏️ Type a few SKUs"],
        index=0,
        help=(
            "Upload a full production schedule (CSV) — best for monthly planning. "
            "Or type a few SKU rows by hand — best for quick what-if scenarios."
        ),
    )

    uploaded = None
    manual_entries = None

    if schedule_mode == "📤 Upload schedule file":
        uploaded = st.sidebar.file_uploader(
            "Choose a CSV",
            type=["csv"],
            help=(
                "Required columns (any of these names will match): LOCATION / "
                "Facility, FG SKU ID / FG SKU, FG ACCRY SKU ID / Accessory SKU, "
                "BUILD QTY / Qty, PRODUCTION MONTH / Month. Extra columns are "
                "ignored. Optional: CUSTOMER NAME."
            ),
            label_visibility="collapsed",
        )
        # Blank template — gives the planner a ready-to-fill CSV with exactly
        # the 5 columns the loader reads, plus a couple of example rows.
        # CUSTOMER NAME is optional and intentionally omitted so the template
        # stays minimal; the loader treats it as a `.get` with empty fallback.
        _template_csv = (
            "LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,PRODUCTION DAY\n"
            "HENDERSON,BOSS25-010,BOSS25-A016,5,May-26,\n"
            "CYPRESS,SDG25,,2,May-26,\n"
        )
        st.sidebar.download_button(
            "📥 Download blank template",
            _template_csv.encode("utf-8"),
            file_name="schedule_template.csv",
            mime="text/csv",
            use_container_width=True,
            help=(
                "Empty CSV with the columns the app needs. Fill in your "
                "schedule and re-upload. **PRODUCTION DAY** is optional — "
                "leave it blank to let the tool auto-spread units across "
                "the month's working days, or fill in dates like "
                "`5/8/2026` to pin your own sequence."
            ),
        )
        st.sidebar.caption(
            "🔒 Uploaded schedules stay in your browser by default. "
            "Use the 📂 panel below to save one to GitHub for the team."
        )

        # Save / load / delete uploaded schedules (per-facility, shared)
        _render_uploaded_schedules_panel(location, uploaded)

        # Validation summary — shows once a schedule has actually loaded.
        # Cleared automatically when the next schedule load attempt happens.
        _summary = st.session_state.get("_last_upload_summary")
        if _summary and isinstance(_summary, dict):
            _src_label = {
                "upload":  "uploaded CSV",
                "saved":   "saved schedule",
                "bundled": "bundled sample",
            }.get(_summary.get("source", ""), "schedule")
            _auto = _summary.get("auto_spread")
            _auto_suffix = ""
            if isinstance(_auto, dict) and _auto.get("count"):
                _auto_month = _auto.get("target_month_label", "")
                _across = f"across **{_auto_month}** " if _auto_month else ""
                _auto_suffix = (
                    f"  \n📅 _Auto-leveled {_auto['count']} of "
                    f"{_auto['total_rows']} undated rows {_across}"
                    f"({_auto['working_days']} working days)._"
                )
            # Note the CSV's own month when it differs from the Plan month, so
            # the planner understands why (e.g. a May export planned for June).
            _csv_month = _summary.get("csv_month", "")
            _shown_month = _summary.get("month", "—")
            _csv_note = ""
            if _csv_month and _csv_month not in ("—", _shown_month):
                _csv_note = f" _(CSV says {_csv_month})_"
            st.sidebar.success(
                f"✅ Loaded **{_summary.get('rows', '?')}** rows from "
                f"**{_summary.get('location', '?')}** "
                f"· month **{_shown_month}**{_csv_note} "
                f"({_src_label})"
                + _auto_suffix
            )
        elif uploaded is None and "_loaded_schedule_buffer" not in st.session_state:
            st.sidebar.info("Using the bundled May 2026 sample schedule.")
    else:
        # Manual mode — auto-spread runs in main() so dates land on the
        # manual rows automatically; the time-window radio below works
        # for both modes.
        st.sidebar.caption(
            "Enter FG SKU, Accessory SKU (optional), and Quantity for each row. "
            "Use **Browse catalog** below to pick from the known SKUs."
        )

        # Cached loaders — same path as main(), no extra I/O
        machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
        acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))

        # Per-location state keys: a seed DataFrame and a revision counter
        # that we bump every time we programmatically inject a row.
        seed_key = f"manual_seed_{location}"
        rev_key = f"manual_rev_{location}"

        default_entries = pd.DataFrame({
            "FG SKU": [""] * 8,
            "Accessory SKU": [""] * 8,
            "Qty": [0] * 8,
        })
        st.session_state.setdefault(seed_key, default_entries)
        st.session_state.setdefault(rev_key, 0)

        # Render the Browse panel (it may bump rev_key + rerun on Add)
        _render_sku_browser(machine_df, acc_df, location, seed_key, rev_key)
        _render_scenarios_panel(location, seed_key, rev_key)

        # Render the data_editor with a rev-suffixed key so a fresh seed
        # is picked up after each programmatic add.
        manual_entries = st.sidebar.data_editor(
            st.session_state[seed_key],
            use_container_width=True,
            num_rows="dynamic",
            key=f"manual_entries_{location}_v{st.session_state[rev_key]}",
            column_config={
                "FG SKU": st.column_config.TextColumn("FG SKU", help="e.g. BOSS25-006"),
                "Accessory SKU": st.column_config.TextColumn(
                    "Accessory SKU", help="e.g. BOSS25-A016 (optional)"
                ),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            },
        )

        # Persist the latest in-flight edits so the next Add appends on top
        st.session_state[seed_key] = manual_entries

    # Plan month — the authoritative month for the whole tool. It drives:
    #   • where auto-spread drops undated rows (target month),
    #   • how the 📆 Calendar divides the month into Monday–Friday weeks,
    #   • the month label shown on the Overview + Calendar.
    # Rows whose PRODUCTION DAY falls in a different month still keep their
    # dates, but the page warns the planner they fall outside this month.
    _now = now_local()
    # Rolling window: 2 months back → 12 months forward, current month first.
    _month_opts: list[tuple[int, int]] = []
    for _delta in range(-2, 13):
        _m = _now.month - 1 + _delta
        _y = _now.year + _m // 12
        _month_opts.append((_y, _m % 12 + 1))
    _month_labels = {
        (y, m): f"{calendar.month_name[m]} {y}" for (y, m) in _month_opts
    }
    _default_idx = _month_opts.index((_now.year, _now.month))
    plan_month = st.sidebar.selectbox(
        "Plan month",
        _month_opts,
        index=_default_idx,
        format_func=lambda ym: _month_labels[ym],
        key="plan_month_select",
        help=(
            "The month this plan is for. Sets how the Calendar splits weeks "
            "(Monday–Friday) and where undated rows get auto-scheduled. "
            "Change this to plan a different month — e.g. pick *June 2026*."
        ),
    )
    plan_month_label = _month_labels[plan_month]
    _plan_working_days = _working_days_in_month(plan_month[0], plan_month[1])
    st.sidebar.caption(
        f"📅 **{plan_month_label}** — {len(_plan_working_days)} working days "
        "(Mon–Fri)."
    )

    # Time window — applies to both upload and manual modes. Once the
    # schedule has PRODUCTION DAY values (either from the CSV or via
    # auto-spread on manual rows), this filter slices the analysis to
    # just the current week / the remaining days.
    time_window = st.sidebar.radio(
        "Time window",
        ["Whole month", "This week", "Remaining (from today)"],
        index=0,
        horizontal=True,
        help=(
            "Slice the analysis to a sub-week of the month so the "
            "recommendations stay relevant as the month progresses. "
            "Reduce 'Working days' below to match if you switch from "
            "Whole month → This week (typically 5)."
        ),
    )

    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 3 — Settings, behind an expander
    # ----------------------------------------------------------------
    with st.sidebar.expander("⏱️ Working-time settings", expanded=False):
        st.caption("How much capacity you have per person per day.")
        # 'Working days' defaults to the chosen Plan month's working-day count
        # so capacity is computed over the same window you're planning. It
        # re-syncs whenever the Plan month changes, but a manual edit sticks
        # until you pick a different month (e.g. set 5 to model a single week).
        _pm_working_days = len(_plan_working_days)
        if st.session_state.get("_work_days_synced_month") != plan_month:
            st.session_state["work_days_input"] = _pm_working_days
            st.session_state["_work_days_synced_month"] = plan_month
        days = st.number_input(
            "Working days in the period",
            min_value=1, max_value=31,
            key="work_days_input",
            help=(
                f"Defaults to the Plan month's working days "
                f"({_pm_working_days} for {plan_month_label}). Override for a "
                "partial period — e.g. set 5 for a single week."
            ),
        )
        shift = st.number_input(
            "Shift length (minutes)",
            min_value=60, max_value=1440,
            value=DEFAULT_SHIFT_MINUTES, step=30,
            help="480 = 8 hours.",
        )
        safety = st.slider(
            "Planning buffer (safety factor)",
            min_value=0.50, max_value=1.00,
            value=DEFAULT_SAFETY_FACTOR, step=0.05,
            help="0.85 leaves a 15% buffer. 1.00 = use 100% of capacity (no buffer).",
        )
        efficiency = st.slider(
            "Productive time (efficiency factor)",
            min_value=0.40, max_value=1.00,
            value=DEFAULT_EFFICIENCY_FACTOR, step=0.025,
            help=(
                "Fraction of the shift that is actually productive — breaks, setup, "
                "and rework reduce it. 1.00 = no loss. Use 0.625 to match the VSM standard."
            ),
        )

    # ----------------------------------------------------------------
    # STEP 4 — Station headcount (per facility), behind an expander
    # ----------------------------------------------------------------
    with st.sidebar.expander(f"👥 Station headcount for {location}", expanded=False):
        st.caption(
            "Edit how many people work at each station. Set **People = 0** "
            "for any station that doesn't exist at this facility."
        )

        crew_default_df = load_facility_crew_df(location)
        # Friendly display: rename columns for the editor
        display_crew = crew_default_df.rename(
            columns={"HC": "People", "Conc": "Stations/Cells", "Crew": "Crew per unit"}
        )

        edited_display = st.data_editor(
            display_crew,
            use_container_width=True,
            num_rows="fixed",
            key=f"crew_editor_{location}",
            column_config={
                "People": st.column_config.NumberColumn(
                    "People", min_value=0, step=1,
                    help="Total headcount available at this station.",
                ),
                "Stations/Cells": st.column_config.NumberColumn(
                    "Stations/Cells", min_value=0, step=1,
                    help="How many units can be worked on at the same time (parallel bays/cells).",
                ),
                "Crew per unit": st.column_config.NumberColumn(
                    "Crew per unit", min_value=1, step=1,
                    help="People assigned to one unit at this station.",
                ),
            },
        )
        # Convert back to internal column names
        edited = edited_display.rename(
            columns={"People": "HC", "Stations/Cells": "Conc", "Crew per unit": "Crew"}
        )

        # Save button
        if st.button(f"💾 Save crew for {location}", use_container_width=True):
            token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
            if not token:
                st.error(
                    "GitHub token not configured. Ask your admin to add "
                    "`github_token` to Streamlit Secrets."
                )
            else:
                try:
                    with st.spinner(f"Saving {location} crew to GitHub..."):
                        save_facility_crew_to_github(location, edited, token)
                    _track_and_flush("crew_save", facility=location)
                    st.success(
                        f"✅ Saved! Everyone sees the new {location} values "
                        "after the app redeploys (~1 min)."
                    )
                except Exception as e:
                    st.error(f"Save failed: {e}")

        # Live totals
        total_hc = int(edited["HC"].sum())
        active_stations = int((edited["HC"] > 0).sum())
        st.markdown(
            f"**👥 Total headcount:** {total_hc}  \n"
            f"**🏭 Active stations:** {active_stations} of {len(edited)}"
        )

    # ----------------------------------------------------------------
    # Help / Glossary — always last
    # ----------------------------------------------------------------
    with st.sidebar.expander("ℹ️ Help & glossary", expanded=False):
        # Five focused tabs replace the previous 85-line wall of markdown.
        # Tabs render cleanly inside expanders and give planners a way to
        # jump straight to the section they need.
        _help_quick, _help_model, _help_catalog, _help_save, _help_admin = st.tabs([
            "🚀 Quick start",
            "📐 How the model works",
            "🏷 Catalog & stations",
            "💾 Saving your work",
            "🔧 For admins",
        ])

        with _help_quick:
            st.markdown(
                """
**Status colors**
🟢 OK · 🟡 Tight · 🟠 Near capacity · 🔴 Over capacity · ⚪ Not at this facility

**Sidebar in three steps**
1. **Pick a facility** — Henderson, Spartanburg, or Cypress. Each has its own crew config.
2. **Tell us what to build** — upload your monthly schedule CSV, or type a few SKUs for a quick what-if.
3. **Tune** — working days, shift length, safety, efficiency, and per-station headcount under the expanders below.

**Where to find common features**

| Want to… | Go to |
|---|---|
| Save your manual build plan | Sidebar → **📂 Save / load scenarios** (when in "Type a few SKUs" mode) |
| Save your uploaded schedule | Sidebar → **📂 Save / load uploaded schedules** (when in "Upload schedule file" mode) |
| Get a blank upload template | Sidebar → **📥 Download blank template** (under the file uploader) |
| Edit labor times | **📁 Data & Setup** tab → Machine / Accessory catalog |
| Add a missing SKU | **📁 Data & Setup** → catalog → **➕ Add a new SKU** |
| See what gets built each week / day | **📆 Calendar** tab |
| Reshape the schedule (move SKUs between dates) | **📁 Data & Setup** → **🗓 Sequence** |
| See bottlenecks + suggested fixes | **🏠 Overview** → 🎯 Recommended actions, or **🔧 Recommendations** tab |

**Tip:** hover any column header in a table to see its specific tooltip.
                """
            )

        with _help_model:
            st.markdown(
                """
### Time units
- **Person-minutes (p-min)** — labor *effort*. 1 person working 1 min = 1 p-min. 2 people working 30 min = 60 p-min. Independent of how many people you assign.
- **Calendar minutes (cal-min)** — wall-clock *elapsed* time. With 1 person doing 60 p-min, cycle time is 60 cal-min. With 2 people working in parallel, the cycle is 30 cal-min.
- **Cycle time** — calendar minutes a unit physically spends at a station = `p-min ÷ Crew per unit`.
- **Lead time (days)** — total calendar days for **one** person to build the whole unit alone = `Total p-min ÷ (shift × efficiency)`.

### People & stations
- **Headcount (HC) / People** — number of people assigned to a station for the whole shift.
- **Stations/Cells (Conc)** — how many units can be worked on at the same time at one station (parallel bays/cells).
- **Crew per unit** — number of people working together on one unit at a station. Drives cycle time.
- **Required HC** — number of people needed to meet the schedule given safety + efficiency.

### Safety & efficiency
- **Safety factor** — planning buffer applied to capacity. `0.85` = leave 15% slack.
- **Efficiency factor** — fraction of the shift that is actually productive. `1.00` = no loss; `0.625` ≈ VSM standard.
- **Effective capacity** = `HC × shift × days × efficiency × safety` person-minutes per period.

### Labor vs Throughput utilization

Each station has **two** utilization checks. The overall flag (🟢/🟡/🟠/🔴) is the worst of the two — because either one can be the actual bottleneck, and the fix is different.

**Labor utilization** — *"Do I have enough people?"*
- `labor_util = labor_demand ÷ labor_capacity`
- `labor_capacity = HC × shift × days × efficiency × safety` (person-minutes available).
- **Fix when red:** hire more, run overtime, extend the work week. Tuned by the **HC** sidebar control.

**Throughput utilization** — *"Do I have enough stations/fixtures/bays?"*
- `thru_util = units_per_day ÷ thru_capacity`
- `thru_capacity = Stations/Cells × (shift ÷ avg_cycle) × efficiency × safety` (units/day the station can physically push through).
- **Fix when red:** add more cells, shorten cycle time, extend working days. **Adding people doesn't help** — the constraint is the work footprint, not the workforce. Tuned by the **Stations/Cells** sidebar control.

**The classic example: Battery Assembly.** Each battery cell takes ~170 calendar minutes to build, regardless of how many people you put around it. If you have 4 cells but the plan needs 50 batteries/day, throughput maxes out at ~11/day — hiring more people does nothing. You need more cells.

The Overview's **🎯 Recommended actions** splits these two cases so you don't waste a hiring request on a throughput bottleneck.

| If this is red | What's pinching | Where to look |
|---|---|---|
| Labor util | Headcount | Sidebar → **People (HC)** |
| Throughput util | Physical capacity | Sidebar → **Stations/Cells** |
| Both | Both — fix the cells first, hire to fill them | Both controls |
                """
            )

        with _help_catalog:
            st.markdown(
                """
### Unit classes
- **STD** — Standard trailer (full assembly with marry)
- **HS** — Head Skid only (no trailer)
- **HT** — Head + Trailer (mount only, no marry)

### SKU naming
- **FG SKU** — finished-good SKU from `machine_clean.csv` (e.g. `BOSS25-006`).
- **Accessory SKU** — accessory kit from `acc_clean.csv` (e.g. `BOSS25-A016`).
- **Bat** — battery count for that FG SKU. PDS/SDG = 0.
- **Last Modified** — date the row was last edited through the app (blank = never).

### Placeholders
- **`XXX` in a SKU** — estimation placeholder. Labor is an estimate, not measured. E.g. `BOSS220PM XXX`, `BOSS25 AXXX`. The Data Quality view flags any `XXX` row automatically.
- Older `-A999` style placeholders may still appear in a few legacy catalog rows; they've been superseded by `XXX` for any new entries.

### Stations (key → display name)
- `Warehouse` = Warehouse (Pick) — pull parts
- `Wire` = Wire Assembly — pre-wiring sub-assembly
- `Battery` = Battery Assembly — battery cells (BOSS only)
- `PMAcc` = PM Acc (Headunit) — PM/Head-unit accessories
- `GenAcc` = Gen Accessories — generator-side accessories
- `ComAcc` = Com Accessories — compressor-side accessories
- `Trailer` = Trailer Assembly — trailer sub-assembly (STD/HT only)
- `AccKIT` = Accessories KIT — kit prep
- `ETO` = Engineering To Order — special engineering (BOSS220/BOSS400)
- `Final` = Final Assembly — mount + marry (where used)
- `PDI / QC / Ship` — Pre-Delivery Inspection · Quality Check · Shipping
                """
            )

        with _help_save:
            st.markdown(
                """
Answers the question planners ask once they care: *"will the team see what I just did?"*

### Save / load scenarios (manual mode)
When you're in **✏️ Type a few SKUs** mode, the sidebar's **📂 Save / load scenarios** expander lets you name and save your manual SKU list. Saved per facility to `data/scenarios.json` on GitHub — anyone planning for the same facility can reload them.

### Save / load uploaded schedules (upload mode)
When you're in **📤 Upload schedule file** mode, the sidebar's **📂 Save / load uploaded schedules** expander lets you save your uploaded CSV under a name. Saved per facility to `data/uploaded_schedules.json` on GitHub. The **raw CSV** is preserved, so any columns the app currently ignores still survive the round-trip for future use.

### Catalog edits
Every **💾 Save** button in **📁 Data & Setup** (Machine catalog, Accessory catalog, Items, Item packages, Reconciliation, Process flow) pushes to GitHub. Other users see the new values after the next ~1 minute Streamlit Cloud redeploy.

### Confirmation pill
After every successful schedule load, the sidebar shows a green ✅ confirmation like *"Loaded 193 rows from HENDERSON · month May-26"* so you can verify the loader understood your file correctly.

### Unknown-SKU warning banner
If your schedule references FG or accessory SKUs that aren't in the catalogs, a yellow banner appears at the **top of the page** listing them. Add the missing SKUs via **📁 Data & Setup → 🗂 Machine catalog → ➕ Add a new machine SKU** (or the equivalent for accessories) and they'll be picked up on the next reload.

### Privacy
Uploaded CSVs stay **only in your browser** unless you explicitly click **💾 Save schedule**. They aren't written to disk on the server or pushed to GitHub by default.
                """
            )

        with _help_admin:
            st.markdown(
                """
### GitHub token
The 💾 Save buttons require a **GitHub Personal Access Token** with the `repo` scope. Add it to **Streamlit Cloud → Settings → Secrets**:

```toml
github_token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxx"
```

If a planner sees *"GitHub token not configured"* when saving, the token is missing or expired. Read-only browsing works without it.

### Wild-CSV upload behavior
The loader auto-matches column names so the team can upload their normal Excel export without reformatting:
- **Required columns** (any of these naming patterns will match): `LOCATION` / `Facility` / `Site`; `FG SKU ID` / `FG SKU` / `Finished Good`; `FG ACCRY SKU ID` / `Accessory SKU` / `Acc SKU`; `BUILD QTY` / `Qty` / `Quantity`; `PRODUCTION MONTH` / `Prod Month` / `Production Date` / `Month`.
- **Optional:** `CUSTOMER NAME` / `Customer` / `Cust`; `PRODUCTION DAY` / `Prod Day` / `Build Date` / `Date`.
- **Extra columns are ignored** — pandas reads them all but the loader only uses the ones it recognises.
- **UTF-8 BOM is stripped** automatically — Excel's default save format on Windows just works.
- **Date formats:** `Mon-YY` (e.g. `May-26`), `YY-Mon` carryover (e.g. `26-Apr`), US slash (`5/8/2026`), and ISO (`2026-05-08`) all parse correctly.
- **Zero-qty rows** are dropped automatically.

If a required column truly isn't there, the planner sees a yellow banner naming the missing column and listing the aliases the loader tried.

### Auto-spread (level-loading dates)
When a planner uploads a schedule without `PRODUCTION DAY` values (or with some rows dated and others blank), the tool **auto-assigns dates** to the blanks so the weekly heatmap has a meaningful baseline.

The algorithm — Longest Processing Time (LPT) — sorts the blank rows by labor weight (sum of per-station p-min × `BUILD QTY`), largest first, then drops each onto the working day with the currently lightest running total. Result: weekly labor is roughly even, no week takes a disproportionate share of heavy units. Existing dated rows are preserved and their load seeds the day totals so the level-loading respects them.

A yellow banner at the top of the page tells the planner when auto-spread fired and how many rows it touched. The pill in the sidebar gets a `📅 Auto-leveled …` suffix.

**To override:** add real dates to your CSV's `PRODUCTION DAY` column and re-upload. Auto-spread won't touch rows that already have a date.

### Sequencer (Suggest + manual reshape)
The Sequencer turns the static schedule into something you can *re-balance* interactively.

**💡 Suggest a level-loaded schedule** — top-of-page expander. Click *Generate proposal* and the tool runs a constraint-aware LPT scheduler:
- Per-row labor weight = sum of per-station p-min × `BUILD QTY` (same path the rest of the analysis uses).
- For each blank row, sorted largest-first, walk candidate days from lightest to heaviest. For each day, check that placing the unit wouldn't tip *any* station above 100% weekly utilization (computed against your current HC, shift, safety, efficiency). Take the first day that passes.
- If no day satisfies the constraints, the row lands on the least-loaded day anyway and is flagged in an *overload diagnosis* showing which stations would be tipped over.

The proposal appears as a side-by-side preview heatmap on the Overview (Current vs Proposed). **✅ Accept** swaps the analysis to use the proposed sequence; **🚫 Discard** drops it.

**🗓 Sequence sub-section** (📁 Data & Setup) — manual reshape:
- *Editable table* — every row's `PRODUCTION DAY` is a date-picker cell. Edit, then click **🔄 Apply edits** to push the changes into the analysis.
- *🔀 Bulk move* form — shift N units of a chosen FG SKU from one week to another in one click. Useful when you want to flatten a single problem week without per-row edits.
- *📥 Download* — reshaped schedule as CSV. Re-upload to save, or push it through the **📂 Save uploaded schedule** panel.

Reshape edits live in your browser session only — they don't persist to GitHub unless you save through the existing 📂 Save panel.

### 📆 Calendar (week & day rollups)
Top-level tab (sits between **🏠 Overview** and **📊 Capacity**). Bucketed view of *what gets built each week and each day* — one expandable card per week of the month showing total units, total labor (p-min), and the top FG SKUs. Open a card to see:
- **Daily breakdown** — one row per working day in that week: units built, labor p-min.
- **Per-station labor mix** — horizontal bar chart showing how the week's labor splits across Warehouse / Wire / Battery / … / Ship. Quick read on where the week's heat goes.
- **SKU detail** — collapsed table with every unit in the week (day, FG, accessory, qty, customer, location).

Use Calendar when you want the *what's being built when* answer; use Sequence when you want to *reshape* the dates.

### 🧹 Customer suffix cleanup
Lives under **📁 Data & Setup → 🧹 Customer suffix cleanup**. ERP exports often label SKUs with a customer-decal tail like `BOSS70-001HRC`, `BOSS25-006ES`, `BOSS70-012UR`. The labor analysis already collapses these to their canonical base internally, but the suffixed text shows in the Sequence editor, the Calendar's "Top SKUs" line, and the downloaded / saved CSV.

This sub-section detects every suffixed FG / Accessory SKU in your loaded schedule, lists them with their base (after stripping) and a ✓ / ✗ for whether the base exists in the catalog, and gives you two actions:
- **🔄 Strip suffixes in loaded schedule** — rewrites your active schedule's SKU columns to the canonical base form. The analysis numbers don't change; only the *displayed* SKUs do. Catalogs are not touched.
- **📥 Download cleaned schedule** — exports the cleaned CSV without committing the change.

Rows whose base isn't in the catalog (orphan suffixes) are left untouched so analysis doesn't silently lose them.

### Blank template
The **📥 Download blank template** button under the file uploader hands the planner an empty CSV with the 5 required headers + 2 example rows. Quickest way to ensure formatting is right.

### Usage analytics
At the very bottom of the sidebar, the **⚙️ Admin** expander shows session counts, top actions, save events, and recent activity pulled from `data/usage_log.jsonl`. Anonymous UUID per browser — no PII collected.

### Redeploy timing
Every catalog / scenario / schedule / flow save triggers a Streamlit Cloud rebuild (~1 minute). Other users see the new values when their browsers reload after that. The 🔄 Refresh banner at the top of the relevant tab tells you when there's an updated copy to pull in mid-session.
                """
            )

    # ----------------------------------------------------------------
    # Hidden admin analytics — discoverable but visually unobtrusive.
    # Sits at the very bottom of the sidebar; regular users won't notice.
    # ----------------------------------------------------------------
    with st.sidebar.expander("⚙️ Admin", expanded=False):
        _render_admin_dashboard()

    return {
        "uploaded": uploaded,
        "manual_entries": manual_entries,
        "schedule_mode": schedule_mode,
        "location": location,
        "safety": safety,
        "efficiency": efficiency,
        "days": days,
        "shift": shift,
        "crew_config": edited,
        "time_window": time_window,
        "plan_month": plan_month,
        "plan_month_label": plan_month_label,
    }


# =============================================================
# Tab renderers
# =============================================================
def _compute_weekly_utilization(schedule_df_full, machine_df, acc_df, inputs):
    """Return a station × week matrix of labor utilization %.

    Used by the Overview tab's weekly heatmap so a planner can spot which
    week of the month is the actual pressure point — even when the
    currently-selected Time window is "Whole month" and would otherwise
    average the pressure out across all 4 weeks.

    Returns None when the schedule has no PRODUCTION DAY data or no rows
    that successfully expand into units.
    """
    if schedule_df_full is None or schedule_df_full.empty:
        return None
    if "WEEK_OF_MONTH" not in schedule_df_full.columns:
        return None
    weeks = sorted(
        int(w) for w in schedule_df_full["WEEK_OF_MONTH"].dropna().unique()
    )
    if not weeks:
        return None

    # Capacity for one week ≈ 5 working days (the standard). We deliberately
    # don't pull this from inputs["days"] because the planner may have set
    # that for the *whole-month* window; we want the heatmap to always show
    # weekly intensity at a fixed reference rate.
    week_days = 5
    out = {}
    for w in weeks:
        slice_df = schedule_df_full[schedule_df_full["WEEK_OF_MONTH"] == w]
        if slice_df.empty:
            continue
        try:
            units_w = expand_schedule(slice_df, machine_df, acc_df)
        except Exception:
            continue
        if units_w.empty:
            continue
        try:
            cap_w = build_capacity_table(
                units_w, inputs["crew_config"], inputs["shift"], week_days,
                inputs["safety"], inputs["efficiency"],
            )
        except Exception:
            continue
        out[f"Wk {w}"] = cap_w["labor_util_safe"]

    if not out:
        return None
    return pd.DataFrame(out).fillna(0)


def tab_overview(units, capacity, batt_type, inputs, schedule_month: str = "", weekly_util=None):
    # =============================================================
    # Compute headline numbers
    # =============================================================
    total_units = len(units)
    total_labor = int(units["total_labor"].sum())
    total_batt = int(units["Bat"].where(units["Battery"] > 0, 0).sum())
    total_hc = int(inputs["crew_config"]["HC"].sum())
    active_stations = int((inputs["crew_config"]["HC"] > 0).sum())
    total_required_hc = int(capacity["required_hc"].sum())
    total_hc_gap = total_required_hc - total_hc

    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    no_station_list = capacity[capacity["overall_status"] == "🔴 NO STATION"].index.tolist()
    near_cap = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    tight = capacity[capacity["overall_status"] == "🟡 TIGHT"].index.tolist()
    ok_stations = capacity[capacity["overall_status"] == "🟢 OK"].index.tolist()

    # =============================================================
    # Big status banner
    # =============================================================
    month_label = f" · {schedule_month}" if schedule_month else ""
    st.markdown(f"### {inputs['location']} Manufacturing{month_label}")

    has_blocker = bool(over_stations or no_station_list)
    has_warning = bool(near_cap)

    if has_blocker:
        blockers = []
        if over_stations:
            blockers.append(f"{len(over_stations)} station(s) **over capacity** ({', '.join(over_stations)})")
        if no_station_list:
            blockers.append(f"{len(no_station_list)} station(s) **not available** here ({', '.join(no_station_list)})")
        st.error(
            "🚨 **Action needed.** " + " · ".join(blockers)
            + "\n\nSee the **Capacity** and **Recommendations** tabs for fixes."
        )
    elif has_warning:
        st.warning(
            f"⚠️ **Watch closely.** {len(near_cap)} station(s) near capacity: "
            f"**{', '.join(near_cap)}**. Small changes could push them over."
        )
    else:
        st.success("✅ **All clear.** Every station is within safe capacity for this plan.")

    st.markdown("---")

    # =============================================================
    # Hero KPIs — what an executive needs at a glance
    # =============================================================
    st.markdown("#### 📦 What this plan builds")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Units to build", f"{total_units:,}",
        help="Total finished-good units in this schedule (or manual entry).",
    )
    c2.metric(
        "Total work", f"{total_labor:,} p-min",
        help="Total person-minutes of labor required across all stations.",
    )
    c3.metric(
        "Batteries required", f"{total_batt:,}",
        help="Total batteries that need to be assembled (BOSS units only).",
    )
    primary_bottleneck = over_stations[0] if over_stations else (
        near_cap[0] if near_cap else (tight[0] if tight else "None")
    )
    c4.metric(
        "Primary bottleneck", primary_bottleneck,
        help="The station closest to or over capacity. Address this first.",
    )

    st.markdown("#### 👥 Headcount picture")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "People you have", f"{total_hc}",
        help=f"Total headcount across {active_stations} active stations.",
    )
    c6.metric(
        "People you need", f"{total_required_hc}",
        help=(
            f"Sum of headcount required at every station to meet this plan, "
            f"using your buffer ({inputs['safety']:.2f}) and "
            f"productive-time settings ({inputs['efficiency']:.3f})."
        ),
    )
    gap_label = "Short" if total_hc_gap > 0 else ("Surplus" if total_hc_gap < 0 else "Even")
    c7.metric(
        gap_label, f"{abs(total_hc_gap)}",
        delta=f"{total_hc_gap:+d}" if total_hc_gap != 0 else None,
        delta_color="inverse",
        help="Difference between people you need and people you have.",
    )
    short_count = int((capacity["hc_gap"] > 0).sum())
    c8.metric(
        "Stations needing more people", f"{short_count}",
        help="Stations where required headcount exceeds what you have today.",
    )

    st.markdown("---")

    # =============================================================
    # Proposed-schedule preview — Sequencer feature. When the planner has
    # generated a proposal but not yet accepted/discarded it, show its
    # weekly heatmap side-by-side with the current one so the comparison
    # is glanceable.
    # =============================================================
    _proposed = st.session_state.get("_proposed_schedule")
    if (
        isinstance(_proposed, pd.DataFrame) and not _proposed.empty
        and weekly_util is not None and not weekly_util.empty
    ):
        # Compute the proposal's weekly heatmap on the fly using the same
        # helper that produced `weekly_util` for the current schedule.
        try:
            # Pull machine_df / acc_df from cached loaders (same path main() uses).
            _machine = _load_machine_df(_csv_mtime("machine_clean.csv"))
            _acc = _load_acc_df(_csv_mtime("acc_clean.csv"))
            proposed_util = _compute_weekly_utilization(
                _proposed, _machine, _acc, inputs,
            )
        except Exception:
            proposed_util = None
        if proposed_util is not None and not proposed_util.empty:
            st.markdown("#### 💡 Current vs proposed (side-by-side)")
            st.caption(
                "Left: your **current** weekly pressure. Right: the Sequencer's "
                "**proposed** level-loaded version. Scroll up to ✅ Accept or 🚫 Discard."
            )

            def _make_heatmap(df_util, title):
                fig = px.imshow(
                    df_util,
                    text_auto=".0%",
                    color_continuous_scale=[
                        (0.00, "#16A34A"),
                        (0.60, "#A3E635"),
                        (0.75, "#FACC15"),
                        (0.90, "#F97316"),
                        (1.00, "#DC2626"),
                    ],
                    zmin=0, zmax=1.5,
                    aspect="auto",
                    labels={"x": "Week", "y": "Station", "color": "Util %"},
                )
                fig.update_layout(
                    title=dict(text=title, font=dict(size=14)),
                    height=min(380, 60 + 26 * max(len(df_util.index), 1)),
                    margin=dict(l=4, r=4, t=40, b=4),
                    coloraxis_showscale=False,
                )
                return fig

            cL, cR = st.columns(2)
            with cL:
                st.plotly_chart(
                    _make_heatmap(weekly_util, "Current"),
                    use_container_width=True,
                    key="overview_heatmap_current",
                )
            with cR:
                st.plotly_chart(
                    _make_heatmap(proposed_util, "Proposed"),
                    use_container_width=True,
                    key="overview_heatmap_proposed",
                )
            st.markdown("---")

    # =============================================================
    # Weekly utilization heatmap — STAGING / Step 1 of the time-dimension
    # work. Renders only when the schedule has a PRODUCTION DAY column;
    # otherwise the helper returns None and we skip it cleanly.
    # =============================================================
    if weekly_util is not None and not weekly_util.empty:
        st.markdown("#### 📅 Weekly pressure")
        st.caption(
            "Labor utilization per station × week (at a 5-working-days-per-week "
            "reference). Spot which week is the bottleneck even when the "
            "whole-month view averages the pressure out."
        )
        # Clip the color scale at 1.5× so spikes don't wash out the lower end.
        # zmin=0 keeps green visible; zmax=1.5 means anything ≥150% looks
        # the same deep red — which is fine since that's "definitely red".
        _hm = px.imshow(
            weekly_util,
            text_auto=".0%",
            color_continuous_scale=[
                (0.00, "#16A34A"),  # green
                (0.60, "#A3E635"),  # light green
                (0.75, "#FACC15"),  # yellow
                (0.90, "#F97316"),  # orange
                (1.00, "#DC2626"),  # red
            ],
            zmin=0, zmax=1.5,
            aspect="auto",
            labels={"x": "Week", "y": "Station", "color": "Util %"},
        )
        _hm.update_layout(
            height=min(400, 60 + 28 * len(weekly_util.index)),
            margin=dict(l=4, r=4, t=8, b=4),
            coloraxis_showscale=False,
        )
        st.plotly_chart(
            _hm, use_container_width=True,
            key="overview_weekly_heatmap_full",
        )
        st.markdown("---")

    # =============================================================
    # Recommended actions — auto-generated from data
    # =============================================================
    st.markdown("#### 🎯 Recommended actions")
    actions = []

    # 0. Empty schedule — don't pretend everything is fine.
    if total_units == 0 or capacity.empty:
        st.info(
            "ℹ️ **No schedule loaded.** Upload a CSV or type a few SKUs in the sidebar "
            "to see recommendations tailored to your plan."
        )
        return

    # Separate over-capacity stations into "throughput-only" vs "needs HC".
    # A red Overall status can come from labor demand exceeding headcount
    # (fix = add people), or from throughput (cells/cycle) exceeding daily
    # need (fix = add stations/cells or extend working days). Telling a
    # planner to "hire" when the constraint is actually fixture cycle time
    # is misleading, so we split them out.
    labor_over_stations = []  # need more headcount
    throughput_over_stations = []  # need more cells / longer days
    for st_disp in over_stations:
        labor_over = str(capacity.loc[st_disp, "labor_status_safe"]) == "🔴"
        thru_over = str(capacity.loc[st_disp, "thru_status_safe"]) == "🔴"
        # If labor is the over-driver, hiring helps; otherwise treat as throughput.
        if labor_over:
            labor_over_stations.append(st_disp)
        elif thru_over:
            throughput_over_stations.append(st_disp)
        else:
            # Edge case (raw-but-not-safe over) — surface as labor anyway.
            labor_over_stations.append(st_disp)

    # 1. Stations missing entirely (HC=0 with demand)
    if no_station_list:
        actions.append(
            f"**Fix facility setup.** {len(no_station_list)} station(s) have demand but 0 headcount: "
            f"{', '.join(no_station_list)}. Either add people in the sidebar or remove those units from the schedule."
        )

    # 2. Labor-driven over-capacity — name the per-station gap
    if labor_over_stations:
        parts = []
        for st_disp in labor_over_stations:
            gap = 0
            try:
                gap = int(capacity.loc[st_disp, "hc_gap"])
            except Exception:
                gap = 0
            if gap > 0:
                parts.append(f"**~{gap} more at {st_disp}**")
            else:
                # Gap math couldn't pin it (rounding or no required_hc) — fall back to a generic call-out.
                parts.append(f"**more capacity at {st_disp}**")
        actions.append(
            f"**Add {', '.join(parts)}** to bring utilization below 100% (with your safety buffer). "
            f"Alternatives: run overtime, extend working days, or defer some units."
        )

    # 3. Throughput-driven over-capacity — different fix
    if throughput_over_stations:
        # Battery Assembly is the canonical case (cell count × cycle time).
        if "Battery Assembly" in throughput_over_stations:
            actions.append(
                "🔋 **Battery throughput is the bottleneck.** Add battery cells "
                "(Stations/Cells) or extend working days — hiring more people at "
                "Battery won't help, because the constraint is the assembly fixture cycle."
            )
            others = [s for s in throughput_over_stations if s != "Battery Assembly"]
        else:
            others = list(throughput_over_stations)
        if others:
            actions.append(
                f"**Throughput is short at {', '.join(others)}** — these stations need more "
                "parallel cells (Stations/Cells in the sidebar) or longer working days. "
                "Adding labor without more cells won't move the cycle."
            )

    # 4. Headcount short overall (but no station is individually red)
    if total_hc_gap > 0 and not labor_over_stations:
        actions.append(
            f"**Plan to add ~{total_hc_gap} people** across the line to comfortably hit this plan."
        )

    # 5. Near-cap warnings (only when nothing is already red)
    if near_cap and not over_stations:
        actions.append(
            f"**Monitor {', '.join(near_cap)}** — these are 90%+ utilized and risky for any schedule change."
        )

    # 6. Surplus
    if total_hc_gap < -3:
        idle = capacity[capacity["labor_util_safe"] < 0.5].index.tolist()
        if idle:
            actions.append(
                f"**Surplus capacity at {', '.join(idle[:3])}** — consider cross-training "
                f"these {abs(total_hc_gap)} people to support bottleneck stations."
            )

    if not actions:
        actions.append("✅ **No action needed** — this plan is well-balanced. Maintain current staffing.")

    for i, action in enumerate(actions, 1):
        st.markdown(f"{i}. {action}")

    st.markdown("---")

    # =============================================================
    # Station status — colored cards instead of a dense table
    # =============================================================
    st.markdown("#### 📊 Station status at a glance")
    st.caption(
        "Each card shows one station. Color = overall status (labor + throughput). "
        "Number = how loaded it is."
    )

    # Build cards in rows of 4
    stations_to_show = list(capacity.index)
    color_map = {
        "🔴 OVER":       ("#E15759", "white"),
        "🔴 NO STATION": ("#999999", "white"),
        "🟠 NEAR-CAP":   ("#F0A04B", "white"),
        "🟡 TIGHT":      ("#F2D75D", "#333"),
        "🟢 OK":         ("#70AD47", "white"),
        "⚪ N/A":         ("#EEEEEE", "#666"),
    }
    for i in range(0, len(stations_to_show), 4):
        cols = st.columns(4)
        for j, st_name in enumerate(stations_to_show[i:i + 4]):
            row = capacity.loc[st_name]
            status = row["overall_status"]
            bg, fg = color_map.get(status, ("#EEEEEE", "#333"))
            util_pct = row["labor_util_safe"] * 100 if not row.get("station_missing") else 0
            hc = int(row["HC"])
            req = int(row["required_hc"])
            status_label = status.split(" ", 1)[1] if " " in status else status
            cols[j].markdown(
                f"""
<div style="background:{bg};color:{fg};padding:12px;border-radius:8px;margin-bottom:8px;">
<div style="font-weight:600;font-size:0.95rem;">{st_name}</div>
<div style="font-size:0.78rem;opacity:0.85;">{status_label}</div>
<div style="font-size:1.6rem;font-weight:700;margin-top:6px;">{util_pct:.0f}%</div>
<div style="font-size:0.78rem;opacity:0.85;">{hc} people · need {req}</div>
</div>
""",
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # =============================================================
    # Battery type mix (kept — visual is clearer than table)
    # =============================================================
    st.markdown("#### 🔋 Batteries needed by type")
    fig = px.bar(
        batt_type, x="batt_type", y="total_batteries", color="batt_type",
        text="total_batteries",
        labels={"batt_type": "Battery type", "total_batteries": "Batteries"},
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(showlegend=False, height=340, margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)


def tab_capacity_vs_demand(capacity, inputs, batt_sku=None, batt_type=None):
    st.header("📊 Capacity vs Demand")
    st.markdown(
        "**Are stations comfortably staffed for this plan?** "
        "Each row is one station. Use the chart for a visual scan, the simple table for the headline numbers."
    )
    st.caption(
        f"Plan settings — shift: {inputs['shift']} min · "
        f"working days: {inputs['days']} · "
        f"buffer: {inputs['safety']:.2f} · "
        f"productive time: {inputs['efficiency']:.3f}"
    )

    # =============================================================
    # Utilization chart (most scannable)
    # =============================================================
    chart_data = capacity.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["labor_util_safe"] * 100,
        name="Labor utilization",
        marker_color="#5B9BD5",
        text=[f"{v*100:.0f}%" for v in chart_data["labor_util_safe"]],
        textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["thru_util_safe"] * 100,
        name="Throughput utilization",
        marker_color="#ED7D31",
        text=[f"{v*100:.0f}%" for v in chart_data["thru_util_safe"]],
        textposition="outside",
    ))
    fig.add_hline(y=100, line_dash="dash", line_color="red", annotation_text="100% (over capacity)")
    fig.add_hline(y=90, line_dash="dot", line_color="orange", annotation_text="90% (near cap)")
    fig.update_layout(
        barmode="group", height=420,
        yaxis_title="Utilization (%)",
        margin=dict(t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # =============================================================
    # Simple, executive-friendly table
    # =============================================================
    st.markdown("#### 📋 Station summary")
    simple = pd.DataFrame(index=capacity.index)
    simple["Status"] = capacity["overall_status"]
    simple["People"] = capacity["HC"].astype(int)
    simple["Need"] = capacity["required_hc"].astype(int)
    simple["Gap"] = capacity["hc_gap"].astype(int)
    simple["Labor used %"] = (capacity["labor_util_safe"] * 100).round(0).astype(int)
    simple["Throughput used %"] = (capacity["thru_util_safe"] * 100).round(0).astype(int)
    simple["Avg cycle (min)"] = capacity["avg_cycle"].round(1)

    st.dataframe(
        simple, use_container_width=True, height=480,
        column_config={
            "Status":  st.column_config.TextColumn("Status", help="Color-coded overall health: 🟢 OK · 🟡 Tight · 🟠 Near cap · 🔴 Over · ⚪ Not at this facility."),
            "People":  st.column_config.NumberColumn("People", help="Headcount you have at this station."),
            "Need":    st.column_config.NumberColumn("Need", help="People you'd need to comfortably meet the plan."),
            "Gap":     st.column_config.NumberColumn("Gap", help="Need − People. Positive = short staffed."),
            "Labor used %": st.column_config.NumberColumn(
                "Labor used %",
                help="How much of the available labor at this station is consumed by the plan. >100% means over capacity.",
                format="%d%%",
            ),
            "Throughput used %": st.column_config.NumberColumn(
                "Throughput used %",
                help="How much of the line/cell time at this station is consumed. >100% means physical bottleneck.",
                format="%d%%",
            ),
        },
    )

    # =============================================================
    # Full detail — for engineers / planners who want all the numbers
    # =============================================================
    with st.expander("🔍 Show full detail (all columns)", expanded=False):
        disp = pd.DataFrame(index=capacity.index)
        disp["HC"] = capacity["HC"]
        disp["Required HC"] = capacity["required_hc"]
        disp["HC gap"] = capacity["hc_gap"]
        disp["Stations/Cells"] = capacity["Conc"]
        disp["Crew/unit"] = capacity["Crew"]
        disp["Labor demand (p-min)"] = capacity["labor_demand"].astype(int)
        disp["Labor capacity (with buffer)"] = capacity["labor_cap_safe"].astype(int)
        disp["Labor util (with buffer)"] = (capacity["labor_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Labor status"] = capacity["labor_status_safe"]
        disp["Labor capacity (raw)"] = capacity["labor_cap_raw"].astype(int)
        disp["Labor util (raw)"] = (capacity["labor_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Avg cycle (min)"] = capacity["avg_cycle"].round(1)
        disp["Units or batteries"] = capacity["units_or_batt"]
        disp["Need / day"] = capacity["need_per_day"].round(2)
        disp["Throughput cap (with buffer)"] = capacity["thru_cap_safe"].round(2)
        disp["Throughput util (with buffer)"] = (capacity["thru_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Throughput status"] = capacity["thru_status_safe"]
        disp["Throughput cap (raw)"] = capacity["thru_cap_raw"].round(2)
        disp["Throughput util (raw)"] = (capacity["thru_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Overall"] = capacity["overall_status"]
        st.dataframe(disp, use_container_width=True, height=480)
        st.caption(
            "**With buffer** = capacity multiplied by your safety + efficiency factors. "
            "**Raw** = theoretical maximum, no buffers applied. "
            "Battery row reports BATTERIES/day, not units/day."
        )

    # ----------------------------------------------------------------
    # 🔋 Battery throughput section (was a top-level tab before)
    # ----------------------------------------------------------------
    if batt_sku is not None and batt_type is not None:
        total_batt = int(batt_sku["total_batteries"].sum()) if not batt_sku.empty else 0
        if total_batt == 0:
            st.markdown("---")
            st.markdown("#### 🔋 Battery throughput")
            st.info(
                f"**No batteries needed for this plan at {inputs.get('location', '')}.** "
                "(Either the schedule has no BOSS units, or this facility doesn't build batteries.)"
            )
        else:
            st.markdown("---")
            with st.expander("🔋 Battery throughput detail", expanded=False):
                tab_battery_throughput(batt_sku, batt_type, capacity, inputs)


def tab_battery_throughput(batt_sku, batt_type, capacity, inputs):
    st.header("🔋 Battery demand & throughput")
    st.markdown(
        "**Can we build enough batteries to meet this plan?** "
        "Batteries are usually the bottleneck — this tab shows demand vs daily capacity."
    )

    total_batt = int(batt_sku["total_batteries"].sum())
    total_units = int(batt_sku["units"].sum())
    avg_per_unit = total_batt / total_units if total_units else 0
    daily_need = total_batt / inputs["days"] if inputs["days"] else 0

    cap_safe = capacity.loc["Battery Assembly", "thru_cap_safe"] if "Battery Assembly" in capacity.index else 0
    cap_raw = capacity.loc["Battery Assembly", "thru_cap_raw"] if "Battery Assembly" in capacity.index else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Batteries to build", f"{total_batt:,}",
        help="Total batteries needed across all units in this plan.",
    )
    c2.metric(
        "Avg per unit", f"{avg_per_unit:.2f}",
        help="Average batteries per BOSS unit (varies — BOSS25 = 1, BOSS220 = 3, BOSS400 = 5).",
    )
    c3.metric(
        "Need / day", f"{daily_need:.2f}",
        help=f"Daily build rate to finish in {inputs['days']} working days.",
    )
    c4.metric(
        "Have / day (with buffer)", f"{cap_safe:.1f}",
        delta=f"{(cap_safe - daily_need):+.1f} vs need",
        delta_color="normal" if cap_safe >= daily_need else "inverse",
        help="Daily capacity at the Battery Assembly cells, with your safety + efficiency buffers applied.",
    )

    if cap_safe < daily_need and cap_safe > 0:
        st.error(
            f"🚨 **Battery throughput is the bottleneck.** "
            f"You need ~{daily_need:.1f}/day but can only do ~{cap_safe:.1f}/day. "
            f"See **Recommendations** for fix options."
        )
    elif cap_safe == 0:
        st.warning(
            "⚠️ Battery Assembly is set to 0 cells/headcount for this facility. "
            "If you need batteries here, configure the station in the sidebar."
        )

    st.markdown("---")
    st.markdown("#### 📋 Battery demand by FG SKU")
    st.dataframe(batt_sku, use_container_width=True, height=360)

    st.markdown("#### 📊 Battery demand by type")
    cc1, cc2 = st.columns([2, 1])
    with cc1:
        fig = px.pie(batt_type, values="total_batteries", names="batt_type",
                     hole=0.45, title="Type mix")
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
    with cc2:
        st.dataframe(batt_type, use_container_width=True)

    # Comparison view
    # =============================================================
    # Plain-English summary
    # =============================================================
    days_needed_safe = (total_batt / cap_safe) if cap_safe else float("inf")
    days_needed_raw = (total_batt / cap_raw) if cap_raw else float("inf")

    st.markdown("#### 📅 How many days to finish all batteries?")
    summary = pd.DataFrame([
        {
            "Scenario": "Realistic (with buffer)",
            "Capacity / day": f"{cap_safe:.1f} batt/day" if cap_safe else "—",
            "Days needed": f"{days_needed_safe:.1f} days" if days_needed_safe != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_safe <= inputs["days"]) else "❌ Over target",
        },
        {
            "Scenario": "Best case (no buffer)",
            "Capacity / day": f"{cap_raw:.1f} batt/day" if cap_raw else "—",
            "Days needed": f"{days_needed_raw:.1f} days" if days_needed_raw != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_raw <= inputs["days"]) else "❌ Over target",
        },
    ])
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.caption(
        f"Your target window is **{inputs['days']} working days**. "
        "The realistic scenario uses your safety + efficiency factors; "
        "the best case assumes everything runs flawlessly."
    )


def tab_mitigation(capacity, batt_sku, units, inputs):
    st.header("🔧 Recommendations")
    st.markdown(
        "**Where to find slack and how to close gaps.** "
        "Use this tab when one or more stations are tight, near, or over capacity."
    )

    # Quick summary of where the issues are
    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    near_stations = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    if over_stations:
        st.error(f"🚨 Over capacity: **{', '.join(over_stations)}**. Address first.")
    if near_stations:
        st.warning(f"⚠️ Near capacity: **{', '.join(near_stations)}**. Monitor.")
    if not over_stations and not near_stations:
        st.success("✅ No stations are over or near capacity — no urgent action needed.")

    st.markdown("---")
    st.markdown("#### 👥 People you could move (rotation candidates)")
    st.caption(
        "Stations sorted from most idle to most loaded. The greener the row, the more "
        "people are free to cross-train and help bottleneck stations."
    )

    idle_rows = []
    for disp, row in capacity.iterrows():
        if row.get("station_missing", False):
            continue
        util = row["labor_util_safe"]
        idle_pct = max(0, 1 - util)
        idle_hc_eq = idle_pct * row["HC"]
        if util > 1:
            tag = "🔴 Bottleneck"
            note = "Needs help — add capacity here"
        elif idle_pct > 0.5:
            tag = "✅ Lots of slack"
            note = f"~{idle_hc_eq:.1f} people equivalent free"
        elif idle_pct > 0.3:
            tag = "🟡 Some slack"
            note = f"{idle_pct*100:.0f}% of capacity is idle"
        else:
            tag = "🟠 Busy"
            note = "Limited rotation potential"
        idle_rows.append({
            "Station": disp,
            "People": int(row["HC"]),
            "Used %": int(round(util * 100)),
            "Idle %": int(round(idle_pct * 100)),
            "Rotation potential": tag,
            "Notes": note,
            "_sort": idle_pct,
        })
    idle_df = pd.DataFrame(idle_rows).sort_values("_sort", ascending=False).drop(columns="_sort")
    st.dataframe(
        idle_df, use_container_width=True, height=420, hide_index=True,
        column_config={
            "Used %": st.column_config.NumberColumn("Used %", format="%d%%"),
            "Idle %": st.column_config.NumberColumn("Idle %", format="%d%%"),
        },
    )

    # =============================================================
    # Fix playbook — scenario-aware (works for any bottleneck)
    # =============================================================
    st.markdown("---")
    st.markdown("#### 🛠️ Standard fix options")
    st.caption(
        "Each option closes a capacity gap differently. Pick the right one based on "
        "whether you need a permanent vs. temporary fix and how much capex you can spend."
    )

    bottleneck = (over_stations[0] if over_stations
                  else (near_stations[0] if near_stations else "the bottleneck station"))

    playbook = pd.DataFrame([
        {
            "#": 1,
            "Option": f"Cross-train and rotate people to {bottleneck}",
            "What it does": "Move people from idle stations to where they're needed.",
            "Effort": "Low — uses the rotation table above",
            "Speed": "Days",
            "When to use": "🟢 Best first move — no capex, immediate",
        },
        {
            "#": 2,
            "Option": "Defer some units to next period",
            "What it does": "Pull the lowest-priority units out of this schedule.",
            "Effort": "Low — schedule change only",
            "Speed": "Immediate",
            "When to use": "🟢 When customer lead time can absorb it",
        },
        {
            "#": 3,
            "Option": "Overtime at the bottleneck",
            "What it does": f"Extra hours per day on {bottleneck}.",
            "Effort": "Low — staffing-only change",
            "Speed": "Days",
            "When to use": "🟡 Temporary fix; budget for OT premium",
        },
        {
            "#": 4,
            "Option": "Add Saturday shifts",
            "What it does": "Extra working days in the month.",
            "Effort": "Medium — touches whole line",
            "Speed": "Weeks",
            "When to use": "🟡 When OT isn't enough; weekend premium",
        },
        {
            "#": 5,
            "Option": "Hire / add a cell at the bottleneck",
            "What it does": "Permanent capacity increase.",
            "Effort": "High — hiring, training, possibly capex",
            "Speed": "Months",
            "When to use": "🟢 If demand is sustained beyond this period",
        },
    ]).set_index("#")
    st.dataframe(playbook, use_container_width=True, height=260)

    st.info(
        "**Tip for planners:** Combine a short-term move (rotation or overtime) "
        "with a structural decision (hiring or scheduling change) so you handle "
        "this month *and* set up the next one for success."
    )


def tab_data_setup(
    machine_df, acc_df, schedule_df, item_master_df, item_packages_df,
    units=None, schedule_df_full=None,
):
    """Consolidated data + admin tab.

    Replaces three previous top-level tabs (Update Labor, Data Quality, Source
    Data) with a single tab containing radio sub-views. Sub-views in order:

      📋 Schedule              — read-only schedule view
      🗂 Machine catalog       — editable catalog + add-new-SKU form
      🗂 Accessory catalog     — editable catalog + add-new + XXX placeholder forms
      🔩 Items                 — unified items table editor
      📦 Item packages         — package definitions editor
      🔬 Reconciliation & Apply — items-vs-aggregate diff + write-back
      🔍 Data Quality          — validation report
    """
    st.header("📁 Data & Setup")
    st.caption(
        "All data inputs in one place. Switch views below to browse the schedule, "
        "edit the catalogs, manage items / packages, reconcile, or run data quality checks."
    )

    sub = st.radio(
        "View",
        [
            "📋 Schedule",
            "🗓 Sequence",
            "🧹 Customer suffix cleanup",
            "🗂 Machine catalog",
            "🗂 Accessory catalog",
            "🔩 Items",
            "📦 Item packages",
            "🔬 Reconciliation & Apply",
            "🔍 Data Quality",
        ],
        horizontal=True,
        label_visibility="collapsed",
    )

    # Used-in-schedule maps (shared by some sub-views)
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    if sub == "📋 Schedule":
        st.subheader("📋 Schedule")
        st.caption("Read-only view of the schedule currently driving the analysis tabs.")
        if schedule_df.empty:
            st.info("No schedule rows. Upload a CSV in the sidebar or switch to manual entry.")
        else:
            st.dataframe(schedule_df, use_container_width=True, height=600)

    elif sub == "🗓 Sequence":
        # Sequence reshape needs the FULL schedule (not the time-window
        # subset) so the planner can move units across all weeks of the
        # month, not just the currently-filtered slice.
        _render_sequencer_view(
            schedule_df_full if schedule_df_full is not None else schedule_df,
        )

    elif sub == "🧹 Customer suffix cleanup":
        # Same pattern as Sequence: cleanup operates on the full schedule.
        _render_suffix_cleanup_view(
            schedule_df_full if schedule_df_full is not None else schedule_df,
            machine_df, acc_df,
        )

    elif sub == "🗂 Machine catalog":
        tab_floor_verification_machine(machine_df, schedule_df, used_fg)

    elif sub == "🗂 Accessory catalog":
        tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df)

    elif sub == "🔩 Items":
        _render_item_master_view(item_master_df)

    elif sub == "📦 Item packages":
        _render_item_packages_view(item_packages_df, item_master_df)

    elif sub == "🔬 Reconciliation & Apply":
        _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc)

    elif sub == "🔍 Data Quality":
        tab_data_validation(machine_df, acc_df, units=units)


def tab_floor_verification_machine(machine_df, schedule_df, used_fg):
    """The Machine-catalog editor — extracted from the old tab_floor_verification
    so it can be embedded as a sub-view of Data & Setup."""
    st.subheader("🗂 Machine catalog")
    _render_stale_data_banner("data/machine_clean.csv")
    st.caption(
        "Edit labor times in the table below. SKUs used in the current schedule "
        "are highlighted. Click **💾 Save** to push changes to GitHub — all users "
        "see new values after redeploy (~1 min)."
    )

    editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "ETO", "Bat"]
    display_df = machine_df.reset_index(drop=True).copy()
    # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
    if "FN_Assy_old" in display_df.columns and "FN_Assy" not in display_df.columns:
        display_df = display_df.rename(columns={"FN_Assy_old": "FN_Assy"})
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
    display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_fg.get(s, 0)))
    display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

    only_used = st.checkbox(
        "Show only SKUs used in current schedule", value=False, key="fv_m_only_used"
    )
    if only_used:
        display_df = display_df[display_df["In schedule"]].copy()
    display_df = display_df.sort_values(
        by=["In schedule", "Used (qty)"], ascending=[False, False]
    ).reset_index(drop=True)

    col_cfg = {
        "SKU": st.column_config.TextColumn(
            "SKU", width="medium",
            help=("Editable — rename to fix a typo. ⚠️ Schedule rows using the "
                  "OLD SKU won't auto-update; re-point or re-upload them."),
        ),
        "Description": st.column_config.TextColumn(
            "Description", width="large",
            help="Editable — change the text and click 💾 Save.",
        ),
        "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
        "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

    edited = st.data_editor(
        display_df,
        use_container_width=True, num_rows="fixed",
        key="fv_machine_editor", height=600,
        column_config=col_cfg, hide_index=True,
    )

    edited_cells, renames = _detect_sku_renames(display_df, edited)
    if renames:
        st.caption("🔁 Pending SKU rename(s): "
                   + ", ".join(f"`{o}` → `{n}`" for o, n in renames))
    _render_pending_changes(edited_cells, machine_df, editable_cols, text_cols=["Description"])

    if st.button("💾 Save updated Machine catalog to GitHub", use_container_width=True):
        _save_catalog_csv(
            edited=edited_cells, source_df=machine_df,
            editable_cols=editable_cols,
            file_path="data/machine_clean.csv", label="machine",
            text_cols=["Description"], renames=renames,
        )

    _render_add_new_sku(
        source_df=machine_df, editable_cols=editable_cols,
        file_path="data/machine_clean.csv", label="machine",
    )


def tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df):
    """The Accessory-catalog editor — extracted from tab_floor_verification."""
    st.subheader("🗂 Accessory catalog")
    _render_stale_data_banner("data/acc_clean.csv")
    st.caption(
        "Edit labor times in the table below. SKUs used in the current schedule "
        "are highlighted. Click **💾 Save** to push changes to GitHub."
    )

    editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                     "PMAcc", "GenAcc", "ComAcc"]
    display_df = acc_df.reset_index(drop=True).copy()
    # Backward-compat: alias "Compressor" → "ComAcc" if cache is stale
    if "Compressor" in display_df.columns and "ComAcc" not in display_df.columns:
        display_df = display_df.rename(columns={"Compressor": "ComAcc"})
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
    display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_acc.get(s, 0)))
    display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

    only_used = st.checkbox(
        "Show only SKUs used in current schedule", value=False, key="fv_a_only_used"
    )
    if only_used:
        display_df = display_df[display_df["In schedule"]].copy()
    display_df = display_df.sort_values(
        by=["In schedule", "Used (qty)"], ascending=[False, False]
    ).reset_index(drop=True)

    # Live computed columns: Bat from machine catalog + Total per unit.
    # Wrap every operand in pd.to_numeric so an Arrow-backed string column
    # (newer pandas default) can't trigger "Can only string multiply by an
    # integer." Each column is coerced through float, NA-filled, before any
    # arithmetic touches it.
    bat_counts = pd.to_numeric(
        display_df["SKU"].astype(str).map(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        ),
        errors="coerce",
    ).fillna(0).astype(int)
    non_batt_cols = [c for c in editable_cols if c != "BattSubRaw"]
    base_numeric = display_df[non_batt_cols].apply(
        lambda col: pd.to_numeric(col, errors="coerce").fillna(0)
    )
    base = base_numeric.sum(axis=1)
    if "BattSubRaw" in display_df.columns:
        per_batt = pd.to_numeric(display_df["BattSubRaw"], errors="coerce").fillna(0)
    else:
        per_batt = pd.Series(0, index=display_df.index)
    display_df["Bat"] = bat_counts.astype(int)
    display_df["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)

    col_cfg = {
        "SKU": st.column_config.TextColumn(
            "SKU", width="medium",
            help=("Editable — rename to fix a typo. ⚠️ Schedule rows using the "
                  "OLD SKU won't auto-update; re-point or re-upload them."),
        ),
        "Description": st.column_config.TextColumn(
            "Description", width="large",
            help="Editable — change the text and click 💾 Save.",
        ),
        "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
        "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
        "Bat": st.column_config.NumberColumn(
            "Bat", disabled=True,
            help="Battery count for this accessory's FG family — from machine_clean.csv.",
        ),
        "Total per unit (p-min)": st.column_config.NumberColumn(
            "Total per unit (p-min)", disabled=True,
            help="Non-battery labor + BattSubRaw × Bat.",
        ),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

    edited = st.data_editor(
        display_df,
        use_container_width=True, num_rows="fixed",
        key="fv_acc_editor", height=600,
        column_config=col_cfg, hide_index=True,
    )
    st.caption(
        "💡 **Total per unit** = non-battery labor + `BattSubRaw × Bat`. "
        "`Bat` is the exact count from the machine catalog (max within the family)."
    )

    edited_cells, renames = _detect_sku_renames(display_df, edited)
    if renames:
        st.caption("🔁 Pending SKU rename(s): "
                   + ", ".join(f"`{o}` → `{n}`" for o, n in renames))
    _render_pending_changes(edited_cells, acc_df, editable_cols, text_cols=["Description"])

    if st.button("💾 Save updated Accessory catalog to GitHub", use_container_width=True):
        _save_catalog_csv(
            edited=edited_cells, source_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv", label="accessory",
            text_cols=["Description"], renames=renames,
        )

    _render_add_new_sku(
        source_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv", label="accessory",
    )

    # XXX-style placeholder helper (replaces the old A999 family placeholder)
    _render_add_xxx_placeholder(
        acc_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv",
    )


def tab_floor_verification(machine_df, acc_df, schedule_df):
    st.header("✅ Floor Verification — Update Labor Catalogs")
    st.caption(
        "Edit labor values directly in the Machine or Accessory catalog. "
        "SKUs used in the current schedule are highlighted. "
        "Click **Save** to push changes to GitHub — all users see the new values "
        "after the app redeploys (~1 min)."
    )

    # Aggregate qty per SKU from the schedule for highlighting
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    catalog = st.radio(
        "Catalog to edit", ["Machine (FG SKU)", "Accessory (Acc SKU)"], horizontal=True,
    )

    # Stale-data banner — polls GitHub for the latest commit SHA of the chosen
    # catalog and warns the user if someone else committed after they loaded.
    active_file = "data/machine_clean.csv" if catalog == "Machine (FG SKU)" else "data/acc_clean.csv"
    _render_stale_data_banner(active_file)

    if catalog == "Machine (FG SKU)":
        # Machine labor columns the user can edit
        editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "Bat"]
        display_df = machine_df.reset_index(drop=True).copy()
        # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
        if "FN_Assy_old" in display_df.columns and "FN_Assy" not in display_df.columns:
            display_df = display_df.rename(columns={"FN_Assy_old": "FN_Assy"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in display_df.columns:
            display_df["Last Modified"] = ""
        display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_fg.get(s, 0)))
        display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="fv_m_only_used"
        )
        if only_used:
            display_df = display_df[display_df["In schedule"]].copy()

        # Sort: used first, then by SKU
        display_df = display_df.sort_values(
            by=["In schedule", "Used (qty)"], ascending=[False, False]
        ).reset_index(drop=True)

        # Column config: read-only for SKU/Description/Last Modified, editable for labor
        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
            "Last Modified": st.column_config.TextColumn(
                "Last Modified", disabled=True,
                help="Date this row was last updated through the app. Blank = never edited.",
            ),
        }
        for c in editable_cols:
            col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

        edited = st.data_editor(
            display_df,
            use_container_width=True,
            num_rows="fixed",
            key="fv_machine_editor",
            height=600,
            column_config=col_cfg,
            hide_index=True,
        )

        # Pending-changes preview so the user can verify their edits before saving
        _render_pending_changes(edited, machine_df, editable_cols)

        # Save button
        if st.button("💾 Save updated Machine catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=machine_df,
                editable_cols=editable_cols,
                file_path="data/machine_clean.csv",
                label="machine",
            )

        # New-SKU form below the editor
        _render_add_new_sku(
            source_df=machine_df,
            editable_cols=editable_cols,
            file_path="data/machine_clean.csv",
            label="machine",
        )

    else:  # Accessory
        editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                         "PMAcc", "GenAcc", "ComAcc"]
        display_df = acc_df.reset_index(drop=True).copy()
        # Backward-compat: alias "Compressor" → "ComAcc" if cache is stale
        if "Compressor" in display_df.columns and "ComAcc" not in display_df.columns:
            display_df = display_df.rename(columns={"Compressor": "ComAcc"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in display_df.columns:
            display_df["Last Modified"] = ""
        display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_acc.get(s, 0)))
        display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="fv_a_only_used"
        )
        if only_used:
            display_df = display_df[display_df["In schedule"]].copy()

        display_df = display_df.sort_values(
            by=["In schedule", "Used (qty)"], ascending=[False, False]
        ).reset_index(drop=True)

        # Live totals — non-battery labor + BattSubRaw × N for N = 1, 3, 5
        # Per-accessory battery count comes from the machine catalog (max Bat for
        # the family). PDS / SDG accessories yield 0 (no battery multiplier).
        bat_counts = display_df["SKU"].astype(str).apply(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        )
        non_batt_cols = [c for c in editable_cols if c != "BattSubRaw"]
        base = display_df[non_batt_cols].fillna(0).sum(axis=1)
        per_batt = display_df["BattSubRaw"].fillna(0)
        display_df["Bat"] = bat_counts.astype(int)
        display_df["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)

        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
            "Bat": st.column_config.NumberColumn(
                "Bat", disabled=True,
                help="Battery count for this accessory's FG family — sourced from machine_clean.csv.",
            ),
            "Total per unit (p-min)": st.column_config.NumberColumn(
                "Total per unit (p-min)", disabled=True,
                help="Non-battery labor + BattSubRaw × Bat. Bat comes from the machine catalog for the accessory's family.",
            ),
            "Last Modified": st.column_config.TextColumn(
                "Last Modified", disabled=True,
                help="Date this row was last updated through the app. Blank = never edited.",
            ),
        }
        for c in editable_cols:
            col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

        edited = st.data_editor(
            display_df,
            use_container_width=True,
            num_rows="fixed",
            key="fv_acc_editor",
            height=600,
            column_config=col_cfg,
            hide_index=True,
        )

        st.caption(
            "💡 **Total per unit** = non-battery labor + `BattSubRaw × Bat`, where "
            "`Bat` is the exact count from the machine catalog for each accessory's "
            "FG family. Family-mixed counts use the maximum (e.g. BOSS220 → 3)."
        )

        # Pending-changes preview so the user can verify their edits before saving
        _render_pending_changes(edited, acc_df, editable_cols)

        if st.button("💾 Save updated Accessory catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=acc_df,
                editable_cols=editable_cols,
                file_path="data/acc_clean.csv",
                label="accessory",
            )

        # New-SKU form below the editor
        _render_add_new_sku(
            source_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv",
            label="accessory",
        )

        # Family-level placeholder helper (e.g. BOSS25-A999)
        _render_add_xxx_placeholder(
            acc_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv",
        )


def _render_pending_changes(edited, source_df, editable_cols, text_cols=None):
    """Show a small expander listing cells where the edited value differs from
    the source. Helps the user confirm their edits are captured before clicking
    Save (and helps debug 'nothing happens' cases)."""
    text_cols = text_cols or []
    if edited is None or len(edited) == 0:
        return
    if "SKU" not in edited.columns:
        return
    try:
        ei = edited.set_index("SKU")
    except Exception:
        return

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.at[sku, col]
            old_val = source_df.at[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append({
                        "SKU": sku, "Column": col,
                        "Old": float(old_val), "New": float(new_val),
                        "Δ": float(new_val) - float(old_val),
                    })
            except (TypeError, ValueError):
                continue
        # Text columns (e.g. Description) — string compare, no numeric Δ.
        for col in text_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_s = "" if pd.isna(ei.at[sku, col]) else str(ei.at[sku, col]).strip()
            old_s = "" if pd.isna(source_df.at[sku, col]) else str(source_df.at[sku, col]).strip()
            if new_s != old_s:
                diffs.append({
                    "SKU": sku, "Column": col,
                    "Old": old_s, "New": new_s, "Δ": "—",
                })

    if not diffs:
        st.caption("ℹ️ No pending edits — make a change in the table above before saving.")
        return

    n = len(diffs)
    with st.expander(f"📝 {n} pending change(s) — preview before saving", expanded=False):
        st.dataframe(pd.DataFrame(diffs), use_container_width=True, hide_index=True)


def _render_add_xxx_placeholder(acc_df, editable_cols, file_path):
    """Quick form to add an estimation placeholder accessory (e.g. `BOSS25 AXXX`).

    Mirrors the machine-catalog `XXX` convention: pick a family, generate
    `{family} AXXX` (with space), and save with default labor times you can
    adjust. Existing examples already in the catalog include `PDS100 AXXX`,
    `BOSS25PM AXXX`, `BOSS220PM AXXX`, etc.

    Note: the model does NOT auto-fall-back to this placeholder when an
    accessory is missing. Some real orders have no accessory component; a
    silent fallback would inflate labor. The placeholder is a regular row
    that the user can OPT to use by typing its SKU in the manual entry. The
    Data Quality view automatically flags any `XXX` SKU as `⚠ Estimation placeholder`.
    """
    with st.expander("➕ Add placeholder accessory (XXX)", expanded=False):
        st.caption(
            "Creates a `{family} AXXX` placeholder accessory row, matching the "
            "machine-catalog XXX naming convention. This is **not** used as an "
            "automatic fallback — it's a conventionally-named accessory you can "
            "reference explicitly."
        )

        family = st.selectbox(
            "FG family",
            options=KNOWN_FAMILIES,
            help="The placeholder SKU will be `{family} AXXX`.",
            key="placeholder_family",
        )
        new_sku = f"{family} AXXX"
        st.text(f"SKU → {new_sku}")

        # Defaults — sensible numbers for the typical family placeholder
        default_labor = {
            "Warehouse": 10,
            "AccKIT": 10,
            "Nameplate Prep": 10,
            "BattSubRaw": 320,
            "PMAcc": 60,
            "GenAcc": 60,
            "ComAcc": 0,
        }

        with st.form("add_xxx_placeholder", clear_on_submit=True):
            new_desc = st.text_input(
                "Description",
                value=f"{family} ESTIMATION ACCESSORY PACKAGE",
                key="placeholder_desc",
            )

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1,
                        value=int(default_labor.get(c, 0)),
                        key=f"placeholder_num_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add `{new_sku}` & save to GitHub",
                use_container_width=True,
            )

        if not submitted:
            return

        # Validate
        existing = {str(s).upper() for s in acc_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(
                f"`{new_sku}` already exists in the accessory catalog. "
                "Edit the existing row in the table above instead."
            )
            return

        token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = today_local_str()
        new_sku_upper = new_sku.upper()
        response = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. Refresh."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add XXX placeholder accessory '{new_sku}' via app",
                        sha=fresh_sha,
                    )
                break
            except GitHubConflict:
                if attempt == 2:
                    st.error("❌ Save failed: concurrent commit. Please refresh and retry.")
                    return
                continue
            except Exception as e:
                st.error(f"❌ Save failed: {e}")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("catalog_add_placeholder", file=file_path, sku=new_sku)
        st.success(
            f"✅ Added XXX placeholder accessory `{new_sku}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIt will appear in the catalog after the app redeploys (~1 min)."
        )


def _render_add_new_sku(source_df, editable_cols, file_path, label, extra_text_cols=None):
    """Form to add a brand-new SKU to a catalog and immediately push to GitHub.

    Args:
      source_df:     The loaded catalog DataFrame (machine_df or acc_df).
      editable_cols: Labor columns the user can fill in (numbers).
      file_path:     Path in the repo (e.g. "data/machine_clean.csv").
      label:         Display label for messages ("machine" or "accessory").
      extra_text_cols: Optional extra text columns to capture (defaults to none).
    """
    extra_text_cols = extra_text_cols or []
    with st.expander(f"➕ Add a new {label} SKU", expanded=False):
        st.caption(
            f"Use this to add a brand-new {label} SKU that isn't in the catalog "
            "yet. The new row is committed to GitHub immediately along with today's "
            "date in **Last Modified**."
        )
        with st.form(f"add_new_{label}", clear_on_submit=True):
            new_sku = st.text_input(
                "SKU", help="Must be unique — case-insensitive check against the catalog.",
                key=f"new_sku_{label}",
            ).strip()
            new_desc = st.text_input(
                "Description", help="Short, descriptive text shown in catalog views.",
                key=f"new_desc_{label}",
            ).strip()

            extra_text_values = {}
            for c in extra_text_cols:
                extra_text_values[c] = st.text_input(
                    c, key=f"new_text_{label}_{c}",
                ).strip()

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1, value=0,
                        key=f"new_num_{label}_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add {label} SKU and save to GitHub", use_container_width=True,
            )

        if not submitted:
            return

        if not new_sku:
            st.error("SKU cannot be empty.")
            return
        # CSV / spreadsheet formula injection guard — values starting with
        # =, +, -, or @ are interpreted as formulas when the CSV is opened
        # in Excel / Sheets and can execute commands. Disallow them.
        if new_sku[:1] in ("=", "+", "-", "@"):
            st.error(
                f"`{new_sku}` cannot start with `=`, `+`, `-`, or `@` — these "
                "trigger formula execution in Excel. Choose a different prefix."
            )
            return
        existing = {str(s).upper() for s in source_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(f"`{new_sku}` is already in the {label} catalog. "
                     "Edit the existing row in the table above instead.")
            return

        token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = today_local_str()
        new_sku_upper = new_sku.upper()

        # Fetch + append + push, with one retry on SHA conflict.
        response = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    # Concurrent-safety check: did someone else just add this SKU?
                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. "
                            "Refresh the page to see it, then edit the existing row instead."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    # Build the new row matching fresh_df's schema
                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in extra_text_values.items():
                        if c in new_row:
                            new_row[c] = v
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add new {label} SKU {new_sku} via app",
                        sha=fresh_sha,
                    )
                break  # success
            except GitHubConflict:
                if attempt == 2:
                    st.error(
                        "❌ Save failed: another user committed concurrently. "
                        "Please refresh the page and try again."
                    )
                    return
                continue
            except Exception as e:
                st.error(f"❌ Save failed: {e}")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("catalog_add_sku", file=file_path, label=label, sku=new_sku)
        st.success(
            f"✅ Added `{new_sku}` to `{file_path}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIt will appear in the catalog after the app redeploys (~1 min)."
        )


@st.cache_data(ttl=30, show_spinner=False)
def _latest_sha_cached(file_path: str, token: str) -> "str | None":
    """Cache the GitHub SHA lookup for 30 seconds to avoid hammering the API."""
    return latest_catalog_sha(file_path, token)


def _render_stale_data_banner(file_path: str) -> None:
    """Show a yellow banner if another user committed to `file_path` after
    we recorded our 'loaded' SHA. The user can click 🔄 to refresh."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        return  # No token → can't poll; silently skip the banner

    state_key = f"loaded_sha::{file_path}"
    # On the first render for this file in this session, record the current SHA
    # as "what the user loaded" — anything newer is stale relative to them.
    if state_key not in st.session_state:
        st.session_state[state_key] = _latest_sha_cached(file_path, token)
        return

    loaded_sha = st.session_state[state_key]
    latest = _latest_sha_cached(file_path, token)
    if latest is None or loaded_sha is None:
        return  # File missing or transient API error — silently skip
    if latest != loaded_sha:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.warning(
                f"⚠️ **`{file_path}` was just updated by another user.** "
                "Your view is stale — refresh before saving so you don't lose their changes."
            )
        with c2:
            if st.button("🔄 Refresh", key=f"stale_refresh_{file_path}"):
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                st.session_state[state_key] = latest
                st.rerun()


def _detect_sku_renames(display_df, edited, key_col="SKU"):
    """Positionally compare the SKU column of the rendered ``display_df`` against
    the editor's ``edited`` output (same row order — the editors use
    ``num_rows="fixed"``) to find inline SKU renames.

    Returns ``(edited_cells, renames)`` where:
      • ``renames`` is a list of ``(old_sku, new_sku)`` for rows whose SKU
        changed (new value non-blank), and
      • ``edited_cells`` is a copy of ``edited`` with the SKU column reset to the
        original values — so the cell/text diff (keyed by the *old* SKU) still
        detects labor/description edits made on a row that's also being renamed.
    """
    if edited is None or key_col not in edited.columns or key_col not in display_df.columns:
        return edited, []
    orig = display_df[key_col].astype(str).str.strip().tolist()
    new = edited[key_col].astype(str).str.strip().tolist()
    if len(orig) != len(new):
        # Shapes diverged unexpectedly — skip rename detection, diff as-is.
        return edited, []
    renames = [(o, n) for o, n in zip(orig, new) if o != n and n]
    edited_cells = edited.copy()
    edited_cells[key_col] = orig
    return edited_cells, renames


def _compute_cell_diffs(edited, source_df, editable_cols, text_cols=None):
    """Return a list of (sku, col, new_value) tuples for cells the user changed.

    Compares `edited` (user's data_editor output) against `source_df` (the
    in-memory catalog loaded at session start).

    `editable_cols` are numeric (compared as floats). `text_cols` (e.g.
    ``["Description"]``) are compared as trimmed strings so free-text columns
    can be edited and saved too.
    """
    text_cols = text_cols or []
    # Drop UI-only / derived columns first. We exclude any name that is also in
    # `editable_cols` so we don't accidentally strip a real editable column
    # (e.g. "Bat" is a derived column in the accessory editor but a real
    # editable column in the machine editor).
    candidate_drops = [
        "Bat", "Total per unit (p-min)",
        "Total (1 batt)", "Total (3 batt)", "Total (5 batt)",
        "Total labor (p-min)",
        "Used (qty)", "In schedule",
    ]
    drop_cols = [c for c in candidate_drops
                 if c in edited.columns and c not in editable_cols]
    work = edited.drop(columns=drop_cols)
    if "SKU" not in work.columns:
        return []
    ei = work.set_index("SKU")

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.loc[sku, col]
            old_val = source_df.loc[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append((sku, col, new_val))
            except (TypeError, ValueError):
                continue
        # Text columns (e.g. Description) — compare as trimmed strings.
        for col in text_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.loc[sku, col]
            old_val = source_df.loc[sku, col]
            new_s = "" if pd.isna(new_val) else str(new_val).strip()
            old_s = "" if pd.isna(old_val) else str(old_val).strip()
            if new_s != old_s:
                diffs.append((sku, col, new_s))
    return diffs


def _apply_diffs_and_serialize(fresh_df, diffs, editable_cols, renames=None):
    """Apply the cell-level diffs (and optional SKU renames) on top of
    `fresh_df` (from GitHub) and return CSV text.

    `diffs` are keyed by the row's *current* (old) SKU. `renames` is a list of
    ``(old_sku, new_sku)`` applied AFTER the cell diffs — so cell edits made on
    a row that's also being renamed still land correctly (they're matched by
    the old SKU first, then the SKU is changed). Stamps Last Modified for any
    row that changed.

    Returns ``(csv_text, n_cell_rows_changed, n_renamed)``.
    """
    renames = renames or []
    today = today_local_str()
    if "Last Modified" not in fresh_df.columns:
        fresh_df["Last Modified"] = ""

    # Build an index lookup. The CSV from GitHub doesn't have SKU as index;
    # we need to find each SKU by its column value.
    if "SKU" not in fresh_df.columns:
        raise RuntimeError("Catalog CSV is missing the SKU column.")
    fresh_df = fresh_df.copy()
    fresh_df["__sku_norm"] = fresh_df["SKU"].astype(str).str.strip()

    changed_skus = set()
    for sku, col, new_val in diffs:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if not mask.any():
            continue
        # Ensure the column exists in the fresh file (e.g. column renames)
        if col not in fresh_df.columns:
            fresh_df[col] = 0
        idx = fresh_df.index[mask][0]
        fresh_df.at[idx, col] = new_val
        changed_skus.add(sku)

    for sku in changed_skus:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if mask.any():
            idx = fresh_df.index[mask][0]
            fresh_df.at[idx, "Last Modified"] = today

    # Apply SKU renames last, matching on the (old) normalized SKU.
    n_renamed = 0
    for old_sku, new_sku in renames:
        mask = fresh_df["__sku_norm"] == str(old_sku).strip()
        if not mask.any():
            continue
        idx = fresh_df.index[mask][0]
        fresh_df.at[idx, "SKU"] = new_sku
        fresh_df.at[idx, "Last Modified"] = today
        fresh_df.at[idx, "__sku_norm"] = str(new_sku).strip()
        n_renamed += 1

    fresh_df = fresh_df.drop(columns=["__sku_norm"])
    return fresh_df.to_csv(index=False), len(changed_skus), n_renamed


def _read_fresh_csv(file_path: str, token: str) -> "tuple[pd.DataFrame, str | None]":
    """Fetch the latest CSV from GitHub and parse it. Returns (df, sha)."""
    csv_bytes, sha = fetch_catalog_csv_from_github(file_path, token)
    if not csv_bytes:
        # First-time write — empty DataFrame is fine (caller will populate)
        return pd.DataFrame(), sha
    # Some CSVs are latin-1 encoded; try utf-8 first, fall back to latin-1
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(csv_bytes), encoding="latin-1")
    return df, sha


def _import_missing_skus_to_catalog(
    file_path: str, label: str, skus: "list[str]", token: str,
) -> "tuple[list[str], list[str]]":
    """Append placeholder rows for ``skus`` to the catalog CSV on GitHub.

    Each new row has the SKU + today's ``Last Modified`` and **blank** labor
    cells — the loaders coerce blanks to 0, so the SKU enters the catalog with
    zero labor that the planner fills in afterwards. This is the "quick import"
    path: it gets the missing SKU into the system so its units stop being
    dropped, without forcing the planner to enter labor times up front.

    Reuses the same fresh-fetch + SHA-locked push + one-retry pattern as
    :func:`_render_add_new_sku`. SKUs already present on the freshly-fetched
    file (someone else added them) and SKUs that would trigger a spreadsheet
    formula (leading ``= + - @``) are skipped.

    Returns ``(added, skipped)`` SKU lists. Raises on a hard save failure so
    the caller can surface the error.
    """
    today = today_local_str()
    added: list[str] = []
    skipped: list[str] = []

    for attempt in (1, 2):
        fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
        if fresh_df.empty or "SKU" not in fresh_df.columns:
            raise RuntimeError(f"Could not read `{file_path}` from GitHub.")

        if "Last Modified" not in fresh_df.columns:
            fresh_df["Last Modified"] = ""

        existing = set(fresh_df["SKU"].astype(str).str.strip().str.upper())

        added = []
        skipped = []
        new_rows = []
        for raw in skus:
            sku = str(raw).strip()
            if not sku:
                continue
            # Formula-injection guard — same rule as the manual add form.
            if sku[:1] in ("=", "+", "-", "@"):
                skipped.append(sku)
                continue
            if sku.upper() in existing:
                skipped.append(sku)
                continue
            row = {col: "" for col in fresh_df.columns}
            row["SKU"] = sku
            row["Last Modified"] = today
            new_rows.append(row)
            added.append(sku)
            existing.add(sku.upper())  # guard against dupes within this batch

        if not new_rows:
            return added, skipped  # nothing new to write

        merged = pd.concat([fresh_df, pd.DataFrame(new_rows)], ignore_index=True)
        csv_text = merged.to_csv(index=False)
        try:
            save_catalog_to_github(
                csv_text, file_path, token,
                message=f"Import {len(added)} missing {label} SKU(s) from schedule via app",
                sha=fresh_sha,
            )
            return added, skipped
        except GitHubConflict:
            if attempt == 2:
                raise
            continue  # someone committed concurrently — refetch and retry

    return added, skipped


def _validate_renames(renames, fresh_df):
    """Validate ``(old, new)`` SKU renames against the freshly-fetched catalog.

    Returns ``(clean_renames, error_message)``. ``error_message`` is None when
    everything is valid. Rules: new SKU non-empty, not a spreadsheet-formula
    prefix, unique within the batch, and not colliding with a *different*
    existing SKU.
    """
    fresh_upper = set(fresh_df["SKU"].astype(str).str.strip().str.upper()) \
        if "SKU" in fresh_df.columns else set()
    clean = []
    seen_new = set()
    for old, new in renames:
        old = str(old).strip()
        new = str(new).strip()
        if not old or old == new:
            continue
        if not new:
            return None, f"`{old}` → (blank): a SKU can't be renamed to an empty value."
        if new[:1] in ("=", "+", "-", "@"):
            return None, (
                f"`{new}` can't start with `=`, `+`, `-`, or `@` "
                "(those trigger formula execution in Excel)."
            )
        if new.upper() in seen_new:
            return None, f"Two rows are being renamed to the same SKU `{new}`."
        # Collision with a different existing SKU (one not being renamed away).
        others = fresh_upper - {old.upper()}
        if new.upper() in others:
            return None, (
                f"`{new}` already exists in the catalog — pick a unique SKU "
                "or edit that row instead."
            )
        seen_new.add(new.upper())
        clean.append((old, new))
    return clean, None


def _save_catalog_csv(edited, source_df, editable_cols, file_path, label,
                      text_cols=None, renames=None):
    """Cell-level merge: compute the user's diff, fetch the latest file from
    GitHub, apply only those cells (and any SKU renames) on top, and push back.
    Retries once on a 409 SHA conflict (the narrow race window where someone
    else committed between our fetch and our PUT).

    `text_cols` (e.g. ``["Description"]``) lets free-text columns be edited and
    saved alongside the numeric labor columns. `renames` is a list of
    ``(old_sku, new_sku)`` for inline SKU renames.
    """
    renames = [r for r in (renames or []) if str(r[0]).strip() != str(r[1]).strip()]
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    diffs = _compute_cell_diffs(edited, source_df, editable_cols, text_cols=text_cols)
    if not diffs and not renames:
        st.info("No changes detected.")
        return

    n_diffs = len(diffs)
    n_ren = len(renames)
    _parts = []
    if n_diffs:
        _parts.append(f"{n_diffs} cells")
    if n_ren:
        _parts.append(f"{n_ren} SKU rename(s)")
    commit_message = f"Update {label} catalog via app ({', '.join(_parts)})"

    response = None
    for attempt in (1, 2):
        try:
            with st.spinner(
                f"Saving {label} catalog to GitHub"
                f"{' — retrying' if attempt == 2 else ''}..."
            ):
                fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                if fresh_df.empty:
                    st.error("Could not fetch the current catalog from GitHub.")
                    return
                clean_renames, rename_err = _validate_renames(renames, fresh_df)
                if rename_err:
                    st.error(f"❌ SKU rename rejected: {rename_err}")
                    return
                csv_text, changed_skus_count, n_renamed = _apply_diffs_and_serialize(
                    fresh_df, diffs, editable_cols, renames=clean_renames,
                )
                response = save_catalog_to_github(
                    csv_text, file_path, token,
                    message=commit_message,
                    sha=fresh_sha,
                )
            break  # success — exit retry loop
        except GitHubConflict:
            if attempt == 2:
                st.error(
                    "❌ Save failed: another user committed concurrently. "
                    "Please refresh the page (your edits are not lost — re-enter them) and try again."
                )
                return
            # else: loop will retry with a fresh fetch
            continue
        except Exception as e:
            st.error(
                f"❌ Save failed: {e}\n\n"
                "Common causes: GitHub token missing the `repo` scope, token expired, "
                "or the repo path in `core/catalog_storage.py` is wrong."
            )
            return

    commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
    commit_url = (response or {}).get("commit", {}).get("html_url", "")
    try:
        st.cache_data.clear()
    except Exception:
        pass

    _track_and_flush(
        "catalog_save", file=file_path, label=label, cells=n_diffs, renames=n_ren,
    )

    _saved_bits = []
    if n_diffs:
        _saved_bits.append(f"{n_diffs} cell change(s)")
    if n_ren:
        _saved_bits.append(f"{n_ren} SKU rename(s)")
    st.success(
        f"✅ Saved {' + '.join(_saved_bits)} to `{file_path}` "
        + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
    )
    if n_ren:
        _ren_list = ", ".join(f"`{o}` → `{n}`" for o, n in renames[:8])
        st.warning(
            f"🔁 **Renamed: {_ren_list}.** Heads up — schedule rows and saved "
            "schedules that still reference the **old** SKU won't auto-update; "
            "they'll show as *missing SKU* until you re-point or re-upload them. "
            "(Catalog-only change.)"
        )
    st.info(
        "⏳ **Streamlit Cloud is now redeploying** with the new values "
        "(~1–2 minutes). The Refresh banner at the top of this tab will let "
        "you reload once it's ready."
    )
    if st.button("🔄 I waited — try reloading now", key=f"reload_after_save_{label}"):
        st.rerun()


def tab_cycle_time(units, inputs, machine_df=None, acc_df=None):
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("⏱ Build Time per Unit")
    st.markdown(
        "**How long does it take to build one of each unit?**  \n"
        "ℹ️ Lead Time assumes a **single person** building the whole unit start-to-finish. "
        "Real production runs in parallel across stations, so the actual elapsed time is much shorter. "
        "Use lead time to compare units to each other, not to predict ship dates.\n\n"
        "• **Total Labor** — sum of person-minutes across every station the unit touches.  \n"
        "• **Machine / Accessory Labor** — that total split by source: the FG (machine) "
        "catalog vs the accessory catalog. They add up to Total Labor.  \n"
        "• **Lead Time (days)** — wall-clock days if **one person** built the whole unit "
        f"alone = `Total Labor ÷ ({inputs['shift']} min × efficiency)`.  \n"
        "• **Sum of Cycles** — wall-clock minutes if every station was done sequentially "
        "with your current Crew settings (upper bound — real flow has parallel sub-assembly)."
    )

    shift = inputs["shift"]
    efficiency = max(inputs["efficiency"], 0.01)  # avoid div-by-zero

    # ---- Build the per-pairing summary ----
    summary_rows = []
    detail_rows = []
    for (fg, acc, cls, bat), grp in units.groupby(["fg_base", "acc", "Class", "Bat"]):
        sample = grp.iloc[0]
        total_labor = 0
        sum_cycles = 0.0
        longest_st = None
        longest_cycle = 0.0
        per_station = {}

        for st_key in STATION_KEYS:
            lbr = sample[st_key]
            if lbr == 0:
                continue
            crew = inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"]
            if st_key == "Final" and cls == "HS":
                cycle = lbr / HS_FINAL_CREW
            else:
                cycle = lbr / crew if crew else 0
            total_labor += lbr
            sum_cycles += cycle
            if cycle > longest_cycle:
                longest_cycle = cycle
                longest_st = STATION_KEY_TO_DISPLAY[st_key]
            per_station[st_key] = (int(lbr), round(cycle, 1))

        # Lead time (days) — assumes 1 person, shift × efficiency productive minutes/day
        lead_days = total_labor / (shift * efficiency) if shift > 0 else 0

        # Split the total by source (FG/machine catalog vs accessory catalog).
        machine_lbr = acc_lbr = None
        if machine_df is not None and acc_df is not None:
            split = unit_labor_split(fg, acc or None, machine_df, acc_df)
            if split is not None:
                machine_lbr = int(round(split["machine"]))
                acc_lbr = int(round(split["acc"]))

        summary_rows.append({
            "FG SKU": fg,
            "ACC SKU": acc or "—",
            "Class": cls,
            "Bat": int(bat),
            "Qty in schedule": len(grp),
            "Total Labor (p-min)": int(total_labor),
            "Machine Labor (p-min)": machine_lbr if machine_lbr is not None else int(total_labor),
            "Acc Labor (p-min)": acc_lbr if acc_lbr is not None else 0,
            "Lead Time (days)": round(lead_days, 2),
            "Sum of Cycles (min)": round(sum_cycles, 1),
            "Longest Station": longest_st or "—",
            "Longest Cycle (min)": round(longest_cycle, 1),
        })
        detail_rows.append({"key": f"{fg} + {acc or '—'}", "per_station": per_station, "cls": cls, "bat": int(bat)})

    summary_df = pd.DataFrame(summary_rows).sort_values(
        by="Total Labor (p-min)", ascending=False,
    ).reset_index(drop=True)

    # ---- Top metrics (across all unit pairings) ----
    if not summary_df.empty:
        avg_labor = summary_df["Total Labor (p-min)"].mean()
        avg_lead = summary_df["Lead Time (days)"].mean()
        slowest = summary_df.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Unique unit pairings", f"{len(summary_df)}")
        c2.metric("Avg labor / unit", f"{int(avg_labor):,} p-min")
        c3.metric("Avg lead time / unit", f"{avg_lead:.1f} days")
        c4.metric(
            "Slowest unit", f"{slowest['FG SKU']}",
            help=f"{int(slowest['Total Labor (p-min)']):,} p-min · {slowest['Lead Time (days)']} days",
        )

    st.markdown("---")
    st.subheader("📋 Summary — one row per unit pairing")
    st.caption("Sorted by Total Labor descending. Each row = one unique FG + Accessory pairing in the current schedule.")
    st.dataframe(
        summary_df,
        use_container_width=True,
        height=380,
        hide_index=True,
        column_config={
            "Total Labor (p-min)": st.column_config.NumberColumn(
                "Total Labor (p-min)",
                help="Person-minutes summed across all stations (Machine + Accessory).",
                format="%d",
            ),
            "Machine Labor (p-min)": st.column_config.NumberColumn(
                "Machine Labor (p-min)",
                help="Labor from the FG (machine) catalog: Warehouse, Wire, Trailer, "
                     "Final, PDI, QC, Ship, ETO, and machine-default battery labor.",
                format="%d",
            ),
            "Acc Labor (p-min)": st.column_config.NumberColumn(
                "Acc Labor (p-min)",
                help="Labor from the accessory catalog: Accessory KIT, PM/Gen/Com "
                     "accessories, accessory Warehouse, and accessory-supplied battery labor.",
                format="%d",
            ),
            "Lead Time (days)": st.column_config.NumberColumn(
                "Lead Time (days)",
                help=f"Total Labor ÷ ({shift} min × {efficiency:.3f} efficiency). "
                     f"How long for one person to build the unit alone.",
                format="%.2f",
            ),
            "Sum of Cycles (min)": st.column_config.NumberColumn(
                "Sum of Cycles (min)",
                help="Sum of cycle times across stations (assumes purely sequential — upper bound).",
                format="%.1f",
            ),
            "Longest Cycle (min)": st.column_config.NumberColumn(
                "Longest Cycle (min)",
                help="Cycle time at the bottleneck station for this unit.",
                format="%.1f",
            ),
        },
    )

    # ---- Per-station drill-down ----
    st.markdown("---")
    st.subheader("🔎 Drill down: per-station breakdown")
    st.caption("Pick a unit pairing to see its labor and cycle time at each station.")

    pairing_choices = [r["key"] for r in detail_rows]
    if pairing_choices:
        chosen = st.selectbox(
            "Unit pairing",
            options=pairing_choices,
            index=0,
        )
        chosen_detail = next(r for r in detail_rows if r["key"] == chosen)

        breakdown_rows = []
        for st_key, (lbr, cycle) in chosen_detail["per_station"].items():
            crew = int(inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"])
            hc_used = HS_FINAL_CREW if (st_key == "Final" and chosen_detail["cls"] == "HS") else crew
            breakdown_rows.append({
                "Station": STATION_KEY_TO_DISPLAY[st_key],
                "Total Labor (p-min)": lbr,
                "Crew working in parallel": hc_used,
                "Cycle Time (min)": cycle,
            })
        breakdown_df = pd.DataFrame(breakdown_rows)
        # Add a TOTAL row
        total_lbr = int(breakdown_df["Total Labor (p-min)"].sum())
        total_cycle = round(breakdown_df["Cycle Time (min)"].sum(), 1)
        total_row = pd.DataFrame([{
            "Station": "🟦 TOTAL",
            "Total Labor (p-min)": total_lbr,
            "Crew working in parallel": "—",
            "Cycle Time (min)": total_cycle,
        }])
        breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)

        st.dataframe(
            breakdown_df, use_container_width=True, hide_index=True, height=460,
            column_config={
                "Total Labor (p-min)": st.column_config.NumberColumn(format="%d"),
                "Cycle Time (min)": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        # Quick read-out for this unit
        lead_days = total_lbr / (shift * efficiency) if shift > 0 else 0
        st.info(
            f"**Build summary for {chosen}:**  \n"
            f"• Total work: **{total_lbr:,} person-minutes** "
            f"(= {total_lbr/60:.1f} person-hours)  \n"
            f"• Lead time (1 person, fully utilized): **{lead_days:.2f} days**  \n"
            f"• Sequential build cycle (with current Crew): **{total_cycle:.0f} cal-min** "
            f"(= {total_cycle/60:.1f} hours, ~{total_cycle/shift:.2f} shifts)"
        )


def _compute_station_depths(nodes, edges):
    """Topological depth per node — used to lay out stages in Graphviz.

    `nodes` = set of station keys that the unit visits.
    `edges` = list of dicts with `From`/`To` keys (already filtered to visited nodes).
    Returns {node: depth}, with depth=0 for nodes that have no incoming edges.
    Nodes in a cycle or unreachable fall back to depth=0 to avoid hanging the layout.
    """
    incoming = {n: [] for n in nodes}
    outgoing = {n: [] for n in nodes}
    for e in edges:
        f, t = e["From"], e["To"]
        if f in nodes and t in nodes:
            incoming[t].append(f)
            outgoing[f].append(t)

    depth = {n: 0 for n in nodes}
    # Topological sort via Kahn's algorithm
    indeg = {n: len(incoming[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    visited = set()
    while queue:
        n = queue.pop(0)
        if n in visited:
            continue
        visited.add(n)
        for t in outgoing[n]:
            if depth[t] < depth[n] + 1:
                depth[t] = depth[n] + 1
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return depth


def tab_process_flow(units_df, machine_df, acc_df, inputs):
    """Visual flow chart of a single unit's path through the workstations.

    The graph is built from edges in `data/process_flow.json`, which the user
    can edit through the panel below the diagram. Edges are filtered per unit
    class (HS / HT / STD) so different products can have different shapes.
    Nodes are auto-layered by topological depth — no hardcoded stages.
    """
    from core.labor_calculator import compute_unit_labor, classify_unit
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("🔀 Process Flow")
    st.markdown(
        "**Where does this unit go on the floor?** Pick an FG SKU "
        "(+ optional Accessory) to see the route. The graph is built from "
        "**editable per-class edges** stored in `data/process_flow.json` — use "
        "the **🔧 Edit process flow** panel below to correct the manufacturing "
        "precedence for your facility."
    )

    # ---- Load edges from JSON -------------------------------------------
    all_edges = _load_process_flow_edges(_csv_mtime("process_flow.json"))

    # ---- SKU selectors ---------------------------------------------------
    fg_options = sorted(machine_df["SKU"].astype(str).unique().tolist())
    if not fg_options:
        st.info("No machine SKUs loaded.")
        return

    default_fg = None
    if units_df is not None and not units_df.empty:
        try:
            default_fg = str(units_df.iloc[0]["fg_base"])
        except Exception:
            default_fg = None
    fg_default_idx = fg_options.index(default_fg) if default_fg in fg_options else 0

    c1, c2 = st.columns(2)
    with c1:
        fg_choice = st.selectbox(
            "FG SKU", fg_options, index=fg_default_idx, key="flow_fg",
        )
    with c2:
        family = _machine_family(fg_choice)
        acc_subset = acc_df[
            acc_df["SKU"].astype(str).apply(_accessory_family_hint).str.upper()
            == family.upper()
        ] if family != "OTHER" else acc_df.iloc[0:0]
        acc_options = ["(none)"] + sorted(acc_subset["SKU"].astype(str).tolist())
        acc_choice = st.selectbox("Accessory SKU", acc_options, key="flow_acc")

    acc_sku = None if acc_choice == "(none)" else acc_choice

    # ---- Compute labor for this pairing ---------------------------------
    labor = compute_unit_labor(fg_choice, acc_sku, machine_df, acc_df)
    if labor is None:
        st.error(f"`{fg_choice}` not found in the machine catalog.")
        return
    cls = labor["Class"]   # "HS" / "HT" / "STD"
    bat = int(labor["Bat"])

    # ---- Filter edges to this unit's class + visited stations ---------
    visited = {k for k in STATION_KEYS if float(labor.get(k, 0) or 0) > 0}
    filtered_edges = [
        e for e in all_edges
        if e.get("Class") in ("All", cls)
        and e.get("From") in visited and e.get("To") in visited
    ]

    if not visited:
        st.warning("This unit has no labor at any station — check the catalog.")
        return

    # ---- Per-station summary ---------------------------------------------
    crew_config = inputs["crew_config"]

    def _crew_for(st_key):
        disp = STATION_KEY_TO_DISPLAY.get(st_key, st_key)
        if disp in crew_config.index:
            try:
                return int(crew_config.loc[disp, "Crew"])
            except Exception:
                return 1
        return 1

    def _cycle(st_key, lbr):
        if lbr <= 0:
            return 0.0
        if st_key == "Final" and cls == "HS":
            return lbr / HS_FINAL_CREW
        crew = _crew_for(st_key)
        return lbr / crew if crew else 0.0

    station_visits = []
    for st_key in STATION_KEYS:
        lbr = float(labor.get(st_key, 0) or 0)
        if lbr <= 0:
            continue
        station_visits.append({
            "key": st_key,
            "name": STATION_KEY_TO_DISPLAY.get(st_key, st_key),
            "labor": int(round(lbr)),
            "cycle": round(_cycle(st_key, lbr), 1),
        })

    # ---- Topological-depth layout ---------------------------------------
    depths = _compute_station_depths(visited, filtered_edges)
    # Group by depth
    by_depth = {}
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        by_depth.setdefault(d, []).append(sv)
    max_depth = max(by_depth.keys()) if by_depth else 0

    # Color: orange if alone at its depth (sequential); blue if has siblings (parallel)
    SEQ_COLOR = "#FFE4B5"   # light orange
    PAR_COLOR = "#BCD6F2"   # light blue

    def _node_id(st_key: str) -> str:
        return f"n_{st_key}"

    def _node_label(sv) -> str:
        return f"{sv['name']}\\n{sv['labor']} p-min · {sv['cycle']:.0f} cal-min"

    # ---- Build the Graphviz DOT -----------------------------------------
    dot_lines = [
        "digraph G {",
        '  rankdir=TB;',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", margin="0.18,0.10"];',
        '  edge [color="#666"];',
    ]

    for d in sorted(by_depth.keys()):
        group = by_depth[d]
        color = PAR_COLOR if len(group) > 1 else SEQ_COLOR
        if len(group) > 1:
            dot_lines.append(f"  {{ rank=same; // depth {d}")
            for sv in group:
                dot_lines.append(
                    f'    {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
                )
            dot_lines.append("  }")
        else:
            sv = group[0]
            dot_lines.append(
                f'  {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
            )

    for e in filtered_edges:
        dot_lines.append(f'  {_node_id(e["From"])} -> {_node_id(e["To"])};')

    dot_lines.append("}")
    dot = "\n".join(dot_lines)

    # ---- Headline metrics + render --------------------------------------
    total_labor = sum(sv["labor"] for sv in station_visits)
    sum_cycles = sum(sv["cycle"] for sv in station_visits)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Class", cls,
              help="HS = head-skid (PM only); HT = head + trailer; STD = standard trailer.")
    c2.metric("Batteries", bat)
    c3.metric("Total labor", f"{total_labor:,} p-min")
    c4.metric("Stations visited", len(station_visits))

    if filtered_edges:
        st.graphviz_chart(dot, use_container_width=True)
    else:
        st.info(
            "No edges connect the stations this unit visits — diagram shows nodes "
            "only. Open **🔧 Edit process flow** below to add precedence edges."
        )
        st.graphviz_chart(dot, use_container_width=True)

    # ---- Per-station breakdown table ------------------------------------
    st.markdown("#### 🔎 Per-station breakdown")
    table_rows = []
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        same_depth = len(by_depth.get(d, []))
        table_rows.append({
            "Stage / depth": d,
            "Stage type": "Parallel" if same_depth > 1 else "Sequential",
            "Station": sv["name"],
            "Labor (p-min)": sv["labor"],
            "Cycle (cal-min)": sv["cycle"],
            "Crew (parallel)": (
                HS_FINAL_CREW if (sv["key"] == "Final" and cls == "HS")
                else _crew_for(sv["key"])
            ),
        })
    breakdown_df = pd.DataFrame(table_rows).sort_values(by="Stage / depth")
    total_row = pd.DataFrame([{
        "Stage / depth": "",
        "Stage type": "🟦 TOTAL",
        "Station": "All stations",
        "Labor (p-min)": total_labor,
        "Cycle (cal-min)": round(sum_cycles, 1),
        "Crew (parallel)": "—",
    }])
    breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)
    st.dataframe(
        breakdown_df, use_container_width=True, hide_index=True, height=420,
    )

    # ---- Editor panel ----------------------------------------------------
    _render_flow_editor(all_edges)


def _render_flow_editor(edges: list) -> None:
    """Editable table of process-flow edges. Save / Reset push to GitHub."""
    from core.constants import STATION_KEYS

    with st.expander("🔧 Edit process flow", expanded=False):
        # Replay any one-shot toast stashed before rerun
        toast = st.session_state.pop("_flow_toast", None)
        if toast:
            level, msg = toast
            (st.success if level == "success" else st.error)(msg)

        st.caption(
            "Each row is one edge **From → To**. Use **Class = All** for edges that "
            "apply to every unit type, or **STD / HT / HS** for class-specific edges. "
            f"**Valid stations**: {', '.join(STATION_KEYS)}. "
            "**Valid classes**: All, STD, HT, HS."
        )

        # Build a clean seed DataFrame; normalize dtypes to avoid the
        # data_editor type-compat error.
        if edges:
            seed = pd.DataFrame(edges, columns=["From", "To", "Class"])
        else:
            seed = pd.DataFrame(columns=["From", "To", "Class"])
        for c in ("From", "To", "Class"):
            if c in seed.columns:
                seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})

        edited = st.data_editor(
            seed,
            num_rows="dynamic",
            use_container_width=True,
            key="flow_editor",
            height=420,
            column_config={
                "From": st.column_config.TextColumn(
                    "From", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "To": st.column_config.TextColumn(
                    "To", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "Class": st.column_config.TextColumn(
                    "Class", help="All / STD / HT / HS", width="small",
                ),
            },
        )

        # Live validation feedback
        bad_count = 0
        valid_stations = set(STATION_KEYS)
        for _, row in edited.iterrows():
            f = str(row.get("From", "") or "").strip()
            t = str(row.get("To", "") or "").strip()
            c = str(row.get("Class", "") or "").strip()
            if not f and not t and not c:
                continue  # blank scratch row
            if (f and f not in valid_stations) \
                    or (t and t not in valid_stations) \
                    or (c and c not in PROCESS_FLOW_VALID_CLASSES):
                bad_count += 1
        if bad_count:
            st.warning(
                f"⚠️ {bad_count} row(s) reference unknown station(s) or class. "
                "These will be silently dropped on save."
            )

        c1, c2 = st.columns([3, 1])
        with c1:
            if st.button(
                "💾 Save process flow to GitHub",
                key="flow_save_btn", use_container_width=True,
            ):
                _flow_save(edited)
        with c2:
            if st.button(
                "🔄 Reset to default",
                key="flow_reset_btn", use_container_width=True,
            ):
                _flow_reset()


def _flow_save(edited_df) -> None:
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Saving process flow to GitHub..."):
            response = save_process_flow_to_github(edited_df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("flow_save")
        st.session_state["_flow_toast"] = (
            "success",
            f"✅ Process flow saved"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Save failed: {e}")


def _flow_reset() -> None:
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Resetting process flow to default..."):
            reset_process_flow_to_default(token)
        try:
            st.cache_data.clear()
        except Exception:
            pass
        _track_and_flush("flow_reset")
        st.session_state["_flow_toast"] = (
            "success", "🔄 Process flow reset to default.",
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Reset failed: {e}")


def tab_data_validation(machine_df, acc_df, units=None):
    st.header("🔍 Data Validation")
    st.caption(
        "Automatic checks over the Machine and Accessory catalogs. Re-runs every "
        "time the app reloads — push updated CSVs to GitHub to clear any issues."
    )

    # ---- Schedule-vs-catalog mismatches (data drops that aren't otherwise visible) ----
    # `expand_schedule` quietly skips schedule rows whose FG isn't in the catalog,
    # and lets units build with zero accessory labor when the accessory isn't found.
    # Surface both here so the planner knows the displayed labor totals are missing
    # those rows.
    skipped_fg: list = []
    unknown_acc: list = []
    if units is not None and hasattr(units, "attrs"):
        skipped_fg = list(units.attrs.get("skipped_fg", []) or [])
        unknown_acc = list(units.attrs.get("unknown_acc", []) or [])

    if skipped_fg:
        st.warning(
            f"⚠️ **{len(skipped_fg)} FG SKU(s) in the schedule are missing from the "
            "machine catalog — their units were dropped from the analysis.**  \n"
            f"Missing: `" + "`, `".join(skipped_fg[:25]) + "`"
            + (f" … (+{len(skipped_fg) - 25} more)" if len(skipped_fg) > 25 else "")
            + "  \nFix by adding these SKUs in **🗂 Machine catalog → ➕ Add a new machine SKU**, "
            "or correct typos in the schedule."
        )
    if unknown_acc:
        st.warning(
            f"⚠️ **{len(unknown_acc)} accessory SKU(s) referenced by the schedule are not "
            "in the accessory catalog — those units were built with zero accessory labor.**  \n"
            f"Missing: `" + "`, `".join(unknown_acc[:25]) + "`"
            + (f" … (+{len(unknown_acc) - 25} more)" if len(unknown_acc) > 25 else "")
            + "  \nFix by adding these SKUs in **🗂 Accessory catalog → ➕ Add a new accessory SKU** "
            "or **➕ Add placeholder accessory (XXX)**."
        )
    if skipped_fg or unknown_acc:
        st.markdown("---")

    issues_df = validate_all(machine_df, acc_df)

    if issues_df.empty:
        if not (skipped_fg or unknown_acc):
            st.success("✅ No issues found in either catalog.")
        else:
            st.info("Catalog content itself looks clean — the warnings above are about schedule rows.")
        return

    # Summary metrics
    err_count = int((issues_df["Severity"] == "🔴 Error").sum())
    warn_count = int((issues_df["Severity"] == "🟡 Warning").sum())
    info_count = int((issues_df["Severity"] == "ℹ️ Info").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("🔴 Errors", err_count, help="Likely bugs — fix these first.")
    c2.metric("🟡 Warnings", warn_count, help="Suspicious — review and confirm.")
    c3.metric("ℹ️ Info", info_count, help="Flags or notes already known about.")

    st.markdown("---")

    # Filter
    sev_filter = st.multiselect(
        "Filter by severity",
        options=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
        default=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
    )
    cat_filter = st.multiselect(
        "Filter by category",
        options=sorted(issues_df["Category"].unique().tolist()),
        default=sorted(issues_df["Category"].unique().tolist()),
    )

    filtered = issues_df[
        issues_df["Severity"].isin(sev_filter) & issues_df["Category"].isin(cat_filter)
    ]
    st.dataframe(filtered, use_container_width=True, height=600, hide_index=True)

    # Download
    csv = filtered.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download issues CSV",
        csv,
        "catalog_issues.csv",
        "text/csv",
    )


def _tokenize_description(description: str) -> set:
    """Split an accessory description into uppercase tokens (word boundaries)."""
    import re
    if not description:
        return set()
    desc_up = str(description).upper()
    tokens = set(re.split(r"[^A-Z0-9]+", desc_up))
    tokens.discard("")
    return tokens


def _detect_items_in_description(description: str, item_master_df) -> list:
    """Unique items from the master whose Abbr appears as a whole token in the description.

    The master may have multiple rows per Abbr (one default + variants). We
    dedupe so each item appears only once in the detected list.
    """
    if item_master_df is None or item_master_df.empty:
        return []
    tokens = _tokenize_description(description)
    out = []
    seen = set()
    for a in item_master_df["Abbr"]:
        s = str(a).strip()
        if not s:
            continue
        u = s.upper()
        if u in tokens and u not in seen:
            seen.add(u)
            out.append(s)
    return out


def _detect_packages_in_description(description: str, packages_df) -> list:
    """Packages whose code appears as a whole token in the description (e.g. CWP, AWP)."""
    if packages_df is None or packages_df.empty:
        return []
    tokens = _tokenize_description(description)
    return [
        str(p).strip()
        for p in packages_df["Package"]
        if str(p).strip() and str(p).strip().upper() in tokens
    ]


def _expand_and_dedupe(items_found, packages_found, packages_df) -> list:
    """Expand each detected package into its components, union with standalone
    items, and dedupe. Preserves first-seen ordering for readable output.

    Example: items=["EBH"], packages=["AWP"] (AWP = CCV,EH,EBH,BHW,HFF,LRH)
    -> ["CCV", "EH", "EBH", "BHW", "HFF", "LRH"]  (EBH counted once)
    """
    seen = set()
    ordered = []

    def _add(abbr):
        a = str(abbr).strip()
        if not a:
            return
        key = a.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(a)

    # Expand packages first so component ordering reflects the package definition
    if packages_df is not None and not packages_df.empty:
        pkg_lookup = {
            str(r["Package"]).strip().upper(): str(r.get("Components", "") or "")
            for _, r in packages_df.iterrows()
        }
    else:
        pkg_lookup = {}

    for pkg in packages_found or []:
        comps = pkg_lookup.get(str(pkg).strip().upper(), "")
        for comp in [c.strip() for c in comps.split(",") if c.strip()]:
            _add(comp)

    # Then standalone items (deduped against package components)
    for it in items_found or []:
        _add(it)

    return ordered


def _accessory_sides(family_hint: str, sku: str) -> list:
    """Which station side(s) this accessory feeds into.

    - PDS family -> ["Compressor"]
    - SDG / BOSS non-HS -> ["Generator"]
    - HS variants (BOSSxxHS / -HS- in family) -> ["Generator", "PM"] so HS units
      with PM-only accessories still get the PM side.
    """
    fam = str(family_hint or "").upper()
    if fam.startswith("PDS"):
        return ["Compressor"]
    sides = ["Generator"]
    if "HS" in fam or "HS" in str(sku or "").upper():
        sides.append("PM")
    return sides


def _is_compressor_family(fg_family_hint: str) -> bool:
    """A FG family is a Compressor if it starts with PDS (portable diesel
    compressor). Generators (SDG) and BOSS are not."""
    s = str(fg_family_hint).upper().strip()
    return s.startswith("PDS")


def _accessory_family_hint(sku: str) -> str:
    """Strip the trailing -A### / -AXXX from an accessory SKU to get the FG family.

    e.g. BOSS25-A016 -> BOSS25;  PDS185EZ-A001 -> PDS185EZ.
    """
    import re
    s = str(sku).strip()
    return re.split(r"-A", s)[0]


# Order matters: longest prefixes first so BOSS220HS-002 resolves to BOSS220
# (not the non-existent BOSS2 or BOSS22).
KNOWN_FAMILIES = ["BOSS400", "BOSS220", "BOSS125", "BOSS70", "BOSS25", "PDS", "SDG"]


def _machine_family(sku: str) -> str:
    """Map an FG SKU to a high-level product family (BOSS25 / BOSS70 / … / PDS / SDG)."""
    s = str(sku or "").upper().strip()
    if not s:
        return "OTHER"
    for fam in KNOWN_FAMILIES:
        if s.startswith(fam):
            return fam
    return "OTHER"


def _machine_class(sku: str, description: str) -> str:
    """Classify a machine SKU into one of:
       ⚠ Placeholder · Hybrid · Power Module · Head Trailer · Standard Trailer · Standard.

    PDS/SDG always classify as 'Standard' (compressor/generator base units).
    BOSS rules apply in this priority: Placeholder > Hybrid > Power Module > Head Trailer > Standard Trailer.
    """
    s = str(sku or "").upper().strip()
    d = str(description or "").upper()
    fam = _machine_family(s)
    if "XXX" in s:
        return "⚠ Placeholder"
    if fam in ("PDS", "SDG"):
        return "Standard"
    if "HYBRID" in d:
        return "Hybrid"
    # "HS" or "PM SKID" / "SKID" indicates a Power Module (head-skid only)
    if "HS" in s.replace(fam, "", 1) or "PM SKID" in d or " SKID " in f" {d} ":
        return "Power Module"
    # "HT" in the SKU after the family prefix indicates a Head + Trailer unit
    if "HT" in s.replace(fam, "", 1):
        return "Head Trailer"
    return "Standard Trailer"


def _family_battery_count(machine_df, family_prefix: str) -> int:
    """Max `Bat` among machine rows whose SKU starts with `family_prefix`.

    Returns 0 when nothing matches (e.g. PDS / SDG / unknown family) — those
    accessories simply skip the battery multiplier in the Total column.

    The machine catalog is the authoritative source for battery counts;
    `BATTERY_COUNT_OVERRIDES` from `core/constants.py` is already baked into
    the loaded `machine_df["Bat"]` by `load_machine_labor()`.
    """
    if not family_prefix or machine_df is None or len(machine_df) == 0:
        return 0
    pf = str(family_prefix).strip().upper()
    if not pf:
        return 0
    skus = machine_df["SKU"].astype(str).str.upper()
    mask = skus.str.startswith(pf)
    if not mask.any():
        return 0
    try:
        return int(machine_df.loc[mask, "Bat"].fillna(0).max())
    except (TypeError, ValueError):
        return 0


def _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc):
    """Reconciliation & Apply — packages expanded, family variants honored, PM included.

    For each accessory:
      1. Detect item abbrevs + package codes in the description
      2. Expand packages -> components, dedupe with standalone items
      3. For each side this accessory feeds, resolve each item's time from the
         unified item master (variant rows override the default row), sum, and
         compare against the aggregate in acc_clean.csv
      4. Let the user select rows to push back into acc_clean.csv.
    """
    st.subheader("🔬 Reconciliation & Apply")
    st.markdown(
        "**Cross-check the catalog aggregate against the item master + packages.**  \n"
        "Packages auto-expand into their components (no double-counting). "
        "Family-specific time variants in the item master override the default row. "
        "When the math looks right, tick rows in the **Apply** column and click "
        "**Apply selected updates** to write the new value back to `acc_clean.csv`."
    )
    st.caption(
        "Side mapping: PDS family → **Compressor** · SDG/BOSS → **Generator** "
        "(plus **PM** for HS-derived accessories). One review row per (SKU, side)."
    )

    cc1, cc2 = st.columns([2, 1])
    with cc1:
        only_used = st.checkbox(
            "Show only accessories used in current schedule", value=True,
            key="recon_only_used",
        )
    with cc2:
        side_filter = st.multiselect(
            "Sides", ["Compressor", "Generator", "PM"],
            default=["Compressor", "Generator", "PM"],
            key="recon_side_filter",
        )

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}

    rows = []
    for sku in acc_df.index:
        if only_used and used_acc.get(sku, 0) == 0:
            continue
        ar = acc_df.loc[sku]
        desc = str(ar.get("Description", ""))
        family_hint = _accessory_family_hint(sku)

        items_found = _detect_items_in_description(desc, item_master_df)
        pkgs_found = _detect_packages_in_description(desc, item_packages_df)
        expanded = _expand_and_dedupe(items_found, pkgs_found, item_packages_df)

        sides = _accessory_sides(family_hint, sku)
        for side in sides:
            if side not in side_filter:
                continue
            agg_col = side_to_agg_col[side]
            try:
                agg_value = float(ar[agg_col]) if agg_col in acc_df.columns else 0.0
            except (TypeError, ValueError):
                agg_value = 0.0

            item_times = []
            for abbr in expanded:
                t = resolve_item_time(abbr, family_hint, side, item_master_df)
                if t > 0:
                    item_times.append((abbr, t))
            items_sum = sum(t for _, t in item_times)
            breakdown = ", ".join(f"{a}={t:.0f}" for a, t in item_times) or "—"
            pkg_label = ", ".join(pkgs_found) if pkgs_found else ""

            diff = items_sum - agg_value
            if abs(diff) < 0.5:
                status = "✅ Match"
            elif items_sum == 0 and agg_value > 0:
                status = "⚪ No items detected"
            elif items_sum > 0 and agg_value == 0:
                status = "⚠️ Items but no aggregate"
            elif diff > 0:
                status = f"🔺 Items higher (+{diff:.0f})"
            else:
                status = f"🔻 Aggregate higher ({diff:.0f})"

            rows.append({
                "Apply": False,
                "SKU": sku,
                "Family": family_hint,
                "Side": side,
                "Aggregate (catalog)": int(round(agg_value)),
                "Sum of items": int(round(items_sum)),
                "Difference": int(round(diff)),
                "Status": status,
                "Packages": pkg_label,
                "Items detected": breakdown,
                "Description": desc,
                "Used in schedule": used_acc.get(sku, 0),
            })

    if not rows:
        st.info("No accessories to show. Uncheck the filter or widen the side selection.")
        return

    rec_df = pd.DataFrame(rows).sort_values(
        by=["Used in schedule", "Difference"], ascending=[False, True],
    ).reset_index(drop=True)

    # Top metrics
    matched = int((rec_df["Status"] == "✅ Match").sum())
    no_items = int((rec_df["Status"] == "⚪ No items detected").sum())
    mismatches = len(rec_df) - matched - no_items

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Review rows", len(rec_df))
    c2.metric("Matches", matched)
    c3.metric("Mismatches", mismatches)
    c4.metric("No items detected", no_items)

    edited = st.data_editor(
        rec_df,
        use_container_width=True, height=520, hide_index=True,
        num_rows="fixed",
        key="recon_editor",
        column_config={
            "Apply": st.column_config.CheckboxColumn(
                "Apply",
                help="Tick rows whose 'Sum of items' you want to write back as the new aggregate.",
            ),
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Family": st.column_config.TextColumn("Family", disabled=True),
            "Side": st.column_config.TextColumn("Side", disabled=True),
            "Aggregate (catalog)": st.column_config.NumberColumn(
                "Aggregate (catalog)", disabled=True,
                help="Current value in acc_clean.csv.",
            ),
            "Sum of items": st.column_config.NumberColumn(
                "Sum of items", disabled=True,
                help="Sum of item times resolved from the master (family variant wins over default), with packages expanded.",
            ),
            "Difference": st.column_config.NumberColumn("Difference", disabled=True),
            "Status": st.column_config.TextColumn("Status", disabled=True),
            "Packages": st.column_config.TextColumn(
                "Packages", disabled=True,
                help="Package codes detected in the description, before expansion.",
            ),
            "Items detected": st.column_config.TextColumn(
                "Items detected (after expansion)", width="large", disabled=True,
            ),
            "Description": st.column_config.TextColumn("Description", width="large", disabled=True),
        },
    )

    # Apply button
    selected = edited[edited["Apply"] == True]  # noqa: E712
    n_sel = len(selected)
    btn_label = f"💾 Apply {n_sel} selected update(s) to SKU aggregate" if n_sel \
                else "💾 Apply selected updates to SKU aggregate"
    if st.button(btn_label, use_container_width=True, disabled=(n_sel == 0)):
        _apply_recon_to_acc_csv(selected, acc_df)

    # Download for offline review
    csv = edited.drop(columns=["Apply"]).to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download reconciliation CSV", csv, "accessory_reconciliation.csv", "text/csv",
    )


def _apply_recon_to_acc_csv(selected_df, acc_df):
    """Take the user's selected reconciliation rows and write the new aggregate
    cell values into acc_clean.csv on GitHub.

    Uses the same fresh-fetch + SHA-locked merge pattern as ``_save_catalog_csv``
    so a concurrent edit on an unrelated cell doesn't get clobbered. The
    in-memory ``acc_df`` is only used to compute the diff (old vs. new value)
    — the actual write is layered on top of a freshly-fetched copy from
    GitHub.
    """
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}
    file_path = "data/acc_clean.csv"

    # Compute the set of (sku, col, new_val) changes the planner selected, then
    # filter out no-ops (where the catalog already matches the items sum).
    diffs: list[tuple] = []
    for _, sel in selected_df.iterrows():
        sku = sel["SKU"]
        side = sel["Side"]
        col = side_to_agg_col.get(side)
        if not col:
            continue
        try:
            new_val = float(sel["Sum of items"])
        except (TypeError, ValueError):
            continue
        if sku not in acc_df.index or col not in acc_df.columns:
            continue
        try:
            old_val = float(acc_df.at[sku, col]) if pd.notna(acc_df.at[sku, col]) else 0.0
        except (TypeError, ValueError):
            old_val = 0.0
        if abs(new_val - old_val) < 0.5:
            continue
        diffs.append((str(sku), col, new_val))

    if not diffs:
        st.info("No actual changes detected (selected rows already match the catalog).")
        return

    today = today_local_str()
    n_diffs = len(diffs)
    response = None
    for attempt in (1, 2):
        try:
            with st.spinner(
                f"Saving {n_diffs} reconciliation cell(s) to acc_clean.csv"
                + (" — retrying" if attempt == 2 else "") + "..."
            ):
                fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                if fresh_df.empty or "SKU" not in fresh_df.columns:
                    st.error("Could not fetch the current accessory catalog from GitHub.")
                    return
                # Apply each diff to the FRESH frame so any unrelated edits
                # another user just committed stay intact.
                fresh_indexed = fresh_df.set_index("SKU", drop=False)
                if "Last Modified" not in fresh_indexed.columns:
                    fresh_indexed["Last Modified"] = ""
                touched_skus: set = set()
                for sku, col, new_val in diffs:
                    if sku not in fresh_indexed.index or col not in fresh_indexed.columns:
                        continue
                    fresh_indexed.at[sku, col] = new_val
                    fresh_indexed.at[sku, "Last Modified"] = today
                    touched_skus.add(sku)
                csv_text = fresh_indexed.drop(columns=["SKU"], errors="ignore").reset_index().to_csv(index=False)
                response = save_catalog_to_github(
                    csv_text, file_path, token,
                    message=f"Apply reconciliation: update {len(touched_skus)} SKU(s), {n_diffs} cell(s)",
                    sha=fresh_sha,
                )
            break  # success
        except GitHubConflict:
            if attempt == 2:
                st.error(
                    "❌ Save failed: another user committed concurrently. "
                    "Please refresh the page (your selections are not lost — re-check them) and try again."
                )
                return
            continue
        except Exception as e:
            st.error(f"❌ Save failed: {e}")
            return

    commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
    commit_url = (response or {}).get("commit", {}).get("html_url", "")
    try:
        st.cache_data.clear()
    except Exception:
        pass
    _track_and_flush("recon_apply", file=file_path, cells=n_diffs)
    st.success(
        f"✅ Wrote {n_diffs} updated cell(s) to acc_clean.csv "
        + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
        + "  \nNew values apply after the app redeploys (~1 min)."
    )


def _render_item_master_view(item_master_df):
    """Unified items table — master defaults, family variants, AND per-accessory
    entries in one editor. Replaces the previous split between Item Master and
    Accessory Item Details views."""
    st.subheader("📒 Items")
    st.markdown(
        "**Unified table** of every installable item — master defaults, "
        "family variants, and per-accessory entries all in one place. Edit "
        "inline and click Save to persist."
    )
    st.caption(
        "Each row defines an item's labor times. Use the **FG family** and "
        "**Accessory SKU** columns to scope the row:\n"
        "- Both blank → **default row** (used when nothing more specific matches)\n"
        "- `FG family` only (e.g. `SDG13`, `PDS185EZ`) → **family variant** (longest prefix wins)\n"
        "- `Accessory SKU` populated → **per-accessory entry** (item belongs to that specific accessory)\n\n"
        "Time columns are person-minutes on each side (Compressor / Generator / PM)."
    )

    # Normalize dtypes so st.data_editor's type checker doesn't complain
    seed = item_master_df.copy()
    for c in ("Abbr", "Description", "FG family", "Accessory SKU", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    for c in ("Time on Compressor (min)", "Time on Generator (min)", "Time on PM (min)"):
        if c in seed.columns:
            seed[c] = pd.to_numeric(seed[c], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        use_container_width=True,
        num_rows="dynamic",
        key="item_master_editor",
        height=560,
        column_config={
            "Abbr": st.column_config.TextColumn(
                "Abbr", help="Short code used in accessory descriptions (e.g. EBH, BHW, TEL).",
                width="small",
            ),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "FG family": st.column_config.TextColumn(
                "FG family",
                help="Leave blank for a default / per-accessory row. Set to a prefix "
                     "like SDG13 or PDS185EZ to make this row a family variant.",
                width="small",
            ),
            "Accessory SKU": st.column_config.TextColumn(
                "Accessory SKU",
                help="Leave blank for default/family rows. Populate (e.g. `BOSS25-A016`) "
                     "to declare this item belongs to a specific accessory.",
                width="small",
            ),
            "Time on Compressor (min)": st.column_config.NumberColumn(
                "Compressor min", min_value=0, step=1,
                help="Install time on a Compressor product (PDS series).",
            ),
            "Time on Generator (min)": st.column_config.NumberColumn(
                "Generator min", min_value=0, step=1,
                help="Install time on a Generator product (SDG / BOSS series).",
            ),
            "Time on PM (min)": st.column_config.NumberColumn(
                "PM min", min_value=0, step=1,
                help="Install time on a PM (head-skid / HS) unit. Used when the "
                     "accessory feeds the PM Acc station.",
            ),
            "Notes": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    if st.button("💾 Save items to GitHub", use_container_width=True):
        _save_simple_csv(edited, "data/item_master.csv", "items")


def _render_item_packages_view(packages_df, item_master_df):
    """Reference table of cold-weather / accessory packages (item bundles)."""
    st.subheader("📦 Item packages")
    st.markdown(
        "**Pre-defined bundles** of items often ordered together (e.g. "
        "Cold Weather Package). Each bundle's Components is a comma-separated "
        "list of item abbreviations from the Item master."
    )

    # Quick check: are all package components defined in the item master?
    if not packages_df.empty and not item_master_df.empty:
        all_abbrs = set(item_master_df["Abbr"].astype(str).str.strip())
        problems = []
        for _, row in packages_df.iterrows():
            comps = [c.strip() for c in str(row.get("Components", "")).split(",") if c.strip()]
            missing = [c for c in comps if c not in all_abbrs]
            if missing:
                problems.append(f"`{row['Package']}` references unknown item(s): {', '.join(missing)}")
        if problems:
            st.warning("⚠️ " + " · ".join(problems))

    # Normalize column dtypes so the editor's type checker is happy
    seed = packages_df.copy()
    for c in ("Package", "Description", "Components", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    if "Total Time (min)" in seed.columns:
        seed["Total Time (min)"] = pd.to_numeric(seed["Total Time (min)"], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        use_container_width=True,
        num_rows="dynamic",
        key="item_packages_editor",
        height=400,
        column_config={
            "Package": st.column_config.TextColumn("Package code", width="small"),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "Total Time (min)": st.column_config.NumberColumn(
                "Total time (min)", min_value=0, step=1,
                help="Total install time for the whole package.",
            ),
            "Components": st.column_config.TextColumn(
                "Components",
                help="Comma-separated abbreviations from the Item master.",
                width="large",
            ),
            "Notes": st.column_config.TextColumn("Notes"),
        },
    )

    if st.button("💾 Save item packages to GitHub", use_container_width=True):
        _save_simple_csv(edited, "data/item_packages.csv", "item packages")


def _save_simple_csv(df, file_path, label):
    """Generic save helper — write a DataFrame as CSV to GitHub."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return
    try:
        # Drop rows where every column is empty/NaN
        clean = df.dropna(how="all").copy()
        for c in clean.select_dtypes(include="object").columns:
            clean[c] = clean[c].astype(str).str.strip()
        csv_text = clean.to_csv(index=False)
        with st.spinner(f"Saving {label} to GitHub..."):
            save_catalog_to_github(
                csv_text, file_path, token,
                message=f"Update {label} via app",
            )
        _track_and_flush("simple_save", file=file_path, label=label)
        st.success(f"✅ Saved {label}! New values apply after the app redeploys (~1 min).")
    except Exception as e:
        st.error(f"Save failed: {e}")


def tab_source_data(machine_df, acc_df, schedule_df,
                     item_master_df, item_packages_df):
    st.header("📁 Source Data")
    st.caption(
        "Raw inputs to the model. SKUs used in the current schedule/manual entry "
        "are highlighted and listed first."
    )

    # Aggregate qty per FG and Accessory SKU from the schedule
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    sub = st.radio(
        "Choose dataset",
        [
            "Schedule",
            "Machine catalog",
            "Accessory catalog",
            "Items",
            "Item packages",
            "Reconciliation & Apply",
        ],
        horizontal=True,
    )

    if sub == "Items":
        _render_item_master_view(item_master_df)
        return
    if sub == "Item packages":
        _render_item_packages_view(item_packages_df, item_master_df)
        return
    if sub == "Reconciliation & Apply":
        _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc)
        return

    if sub == "Schedule":
        st.dataframe(schedule_df, use_container_width=True, height=600)
    elif sub == "Machine catalog":
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="m_only_used"
        )
        m_disp = machine_df.copy()
        # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
        if "FN_Assy_old" in m_disp.columns and "FN_Assy" not in m_disp.columns:
            m_disp = m_disp.rename(columns={"FN_Assy_old": "FN_Assy"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in m_disp.columns:
            m_disp["Last Modified"] = ""
        # Per-machine total labor across the assembly stations.
        # Bat is a COUNT (not labor) so it's excluded from the sum.
        machine_labor_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy",
                              "PDI", "QC", "Ship"]
        present_cols = [c for c in machine_labor_cols if c in m_disp.columns]
        m_disp["Total labor (p-min)"] = m_disp[present_cols].fillna(0).sum(axis=1).astype(int)
        m_disp.insert(0, "Used (qty)", m_disp.index.map(lambda s: used_fg.get(s, 0)))
        m_disp.insert(1, "In schedule", m_disp["Used (qty)"] > 0)
        if only_used:
            m_disp = m_disp[m_disp["In schedule"]]
        # Sort: used first (by qty desc), then alphabetical
        m_disp = m_disp.sort_values(
            by=["In schedule", "Used (qty)"],
            ascending=[False, False],
        )
        # Highlight rows that are in the schedule
        def _highlight_machine(row):
            return ["background-color: #FFF3CD"] * len(row) if row["In schedule"] else [""] * len(row)
        st.dataframe(
            m_disp.style.apply(_highlight_machine, axis=1),
            use_container_width=True, height=600,
        )
        st.caption(
            f"🟡 highlighted = used in current schedule "
            f"({sum(m_disp['In schedule'])} of {len(m_disp)} shown). "
            "**Total labor** = sum of Warehouse + Wire + Trailer + Final + PDI + QC + Ship "
            "(person-minutes per unit, excluding accessory + battery labor)."
        )
    else:  # Accessory catalog
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="a_only_used"
        )
        a_disp = acc_df.copy()
        # Backward-compat: if the loaded DataFrame has the old "Compressor"
        # column (because cache hasn't refreshed), alias it to "ComAcc".
        if "Compressor" in a_disp.columns and "ComAcc" not in a_disp.columns:
            a_disp = a_disp.rename(columns={"Compressor": "ComAcc"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in a_disp.columns:
            a_disp["Last Modified"] = ""
        a_disp.insert(0, "Used (qty)", a_disp.index.map(lambda s: used_acc.get(s, 0)))
        a_disp.insert(1, "In schedule", a_disp["Used (qty)"] > 0)
        # Per-accessory total uses the EXACT battery count from machine_clean.csv
        # for the family. PDS / SDG accessories → 0 (no battery multiplier).
        acc_labor_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                          "PMAcc", "GenAcc", "ComAcc"]
        # Only sum columns that actually exist (defensive against stale cache)
        present_labor_cols = [c for c in acc_labor_cols if c in a_disp.columns]
        non_batt_cols = [c for c in present_labor_cols if c != "BattSubRaw"]
        bat_counts = a_disp.index.to_series().astype(str).apply(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        )
        base = a_disp[non_batt_cols].fillna(0).sum(axis=1)
        per_batt = a_disp["BattSubRaw"].fillna(0) if "BattSubRaw" in a_disp.columns else pd.Series(0, index=a_disp.index)
        a_disp["Bat"] = bat_counts.astype(int)
        a_disp["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)
        if only_used:
            a_disp = a_disp[a_disp["In schedule"]]
        a_disp = a_disp.sort_values(
            by=["In schedule", "Used (qty)"],
            ascending=[False, False],
        )
        def _highlight_acc(row):
            return ["background-color: #FFF3CD"] * len(row) if row["In schedule"] else [""] * len(row)
        st.dataframe(
            a_disp.style.apply(_highlight_acc, axis=1),
            use_container_width=True, height=600,
        )
        st.caption(
            f"🟡 highlighted = used in current schedule "
            f"({sum(a_disp['In schedule'])} of {len(a_disp)} shown). "
            "**Total per unit** = non-battery labor + `BattSubRaw × Bat`. "
            "`Bat` is the exact count from the machine catalog for the accessory's FG family."
        )


# =============================================================
# Main
# =============================================================
def main():
    st.title("🏭 Labor Capacity Planner")
    st.caption(
        "Plan production capacity across facilities. "
        "Set up your scenario in the sidebar, then review the tabs below."
    )

    inputs = render_sidebar()

    # Once-per-session "user opened the tool" event. Force-flush so it lands
    # in usage_log.jsonl within seconds of the first interaction.
    if not st.session_state.get("_session_logged"):
        _track_and_flush(
            "session_start",
            facility=inputs.get("location"),
            mode=inputs.get("schedule_mode"),
        )
        st.session_state["_session_logged"] = True

    # Load data with cache
    machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
    acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))
    item_master_df = _load_item_master_df(_csv_mtime("item_master.csv"))
    item_packages_df = _load_item_packages_df(_csv_mtime("item_packages.csv"))

    # ----------------------------------------------------------------
    # Load schedule (manual or CSV). If empty / invalid, we don't return —
    # instead we fall through with empty placeholders so the tabs still
    # render and the user can edit data via the 📁 Data & Setup tab.
    # ----------------------------------------------------------------
    schedule_df = pd.DataFrame()
    empty_banner_msg = None

    if inputs.get("schedule_mode") == "✏️ Type a few SKUs":
        manual_entries = inputs.get("manual_entries")
        if manual_entries is None or manual_entries.empty:
            empty_banner_msg = (
                "✏️ **Manual entry mode** — fill in FG SKU and Quantity in the sidebar to populate "
                "the analysis tabs. You can still use **📁 Data & Setup** to edit catalogs."
            )
        else:
            machine_skus = set(machine_df["SKU"])
            schedule_df = build_manual_schedule(
                manual_entries, location=inputs["location"], machine_skus=machine_skus,
            )
            if schedule_df.empty:
                empty_banner_msg = (
                    "✏️ **Manual entry mode** — fill in at least one row (FG SKU + Quantity > 0) in "
                    "the sidebar. You can still use **📁 Data & Setup** to edit catalogs."
                )
            else:
                # Manual rows don't carry a PRODUCTION DAY — run auto-spread
                # so the Calendar tab, the Sequencer Suggest button, and the
                # weekly heatmap all light up the same way they do for an
                # uploaded CSV. The sidebar "Plan month" picker is the
                # authoritative target month; fall back to a scenario-name
                # guess only if the picker is somehow missing.
                _hint = inputs.get("plan_month")
                if _hint is None:
                    _last_scen = st.session_state.get(
                        f"_last_loaded_scenario_name_{inputs['location']}", ""
                    )
                    _hint = _parse_month_from_name(_last_scen)
                _auto_spread_dates(
                    schedule_df, machine_df, acc_df, inputs["location"],
                    target_month_hint=_hint,
                )
    else:
        try:
            schedule_df = _load_schedule_df(
                inputs["uploaded"], location=inputs["location"],
                target_month_hint=inputs.get("plan_month"),
            )
        except ScheduleColumnError as e:
            # Planner-friendly message with no internals leaked.
            empty_banner_msg = (
                f"📤 **We couldn't read your uploaded schedule.**  \n{e}  \n"
                "You can still use **📁 Data & Setup** to browse and edit catalogs."
            )
            schedule_df = pd.DataFrame()
        if schedule_df.empty and not empty_banner_msg:
            empty_banner_msg = (
                f"⚠️ No schedule rows found for **{inputs['location']}**. "
                "Upload a CSV that contains this location, or select a different location. "
                "You can still use **📁 Data & Setup** to edit catalogs."
            )

    # ----------------------------------------------------------------
    # Sequencer — Suggest button + accept/discard preview. Renders an
    # expander near the top of the page when a schedule is loaded. If
    # the planner has accepted a proposal, the accepted schedule was
    # already pushed into the upload buffer + a rerun fired, so by the
    # time we reach here it's the new sequence we're analyzing.
    # ----------------------------------------------------------------
    schedule_df = _render_sequencer_controls(
        schedule_df, machine_df, acc_df, inputs,
    )

    # ----------------------------------------------------------------
    # Time-window filter (Step 1 of the actuals/time-dimension work).
    # When the planner picks "This week" or "Remaining (from today)", we
    # subset the schedule to just the rows whose PRODUCTION DAY falls in
    # that window. The full schedule is preserved on `schedule_df_full`
    # for the weekly heatmap rendering.
    # ----------------------------------------------------------------
    schedule_df_full = schedule_df.copy() if not schedule_df.empty else schedule_df
    if not schedule_df.empty and "PRODUCTION DAY" in schedule_df.columns:
        window = inputs.get("time_window", "Whole month")
        # Detect the schedule's actual month so "This week" / "Remaining"
        # only fire when today is inside that month. Otherwise the filter
        # would compare today's calendar week (May wk 4) against a future
        # schedule's week numbers (June rows in week 4) — silently wrong.
        _now = now_local()
        _today_date = _now.date()
        _today_ts = pd.Timestamp(_today_date)
        _sched_months = set(
            schedule_df["PRODUCTION DAY"].dropna().dt.to_period("M").astype(str)
        ) if "PRODUCTION DAY" in schedule_df.columns else set()
        _today_period = f"{_now.year}-{_now.month:02d}"
        _today_in_schedule_month = _today_period in _sched_months

        if window == "This week":
            if not _today_in_schedule_month:
                empty_banner_msg = (
                    "📅 **\"This week\" was ignored** — today isn't inside "
                    "this schedule's month. Switch the **Time window** "
                    "selector back to *Whole month* (or pick a date inside "
                    "the schedule's month for the filter to work)."
                )
            else:
                current_week = week_of_month(_today_date)
                schedule_df = schedule_df[
                    schedule_df["WEEK_OF_MONTH"] == current_week
                ].copy()
                if schedule_df.empty and not empty_banner_msg:
                    empty_banner_msg = (
                        f"📅 **No units scheduled for week {current_week}** "
                        f"in {inputs['location']}'s plan. Switch the **Time window** "
                        "selector in the sidebar back to *Whole month* to see the full plan."
                    )
        elif window == "Remaining (from today)":
            schedule_df = schedule_df[
                schedule_df["PRODUCTION DAY"].notna()
                & (schedule_df["PRODUCTION DAY"] >= _today_ts)
            ].copy()
            if schedule_df.empty and not empty_banner_msg:
                if not _today_in_schedule_month and _sched_months:
                    empty_banner_msg = (
                        "📅 **No remaining units from today onward.** "
                        "This schedule is for a different month than today — "
                        "switch back to *Whole month* to see it."
                    )
                else:
                    empty_banner_msg = (
                        "📅 **No remaining units scheduled from today onward.** "
                        "Switch the **Time window** selector to *Whole month* "
                        "to see the full plan."
                    )

    # ----------------------------------------------------------------
    # Compute analysis frames. Empty-safe: when there are no units, we
    # construct empty placeholders so downstream tabs can detect the
    # empty state and show their own banner instead of crashing.
    # ----------------------------------------------------------------
    if schedule_df.empty:
        units = pd.DataFrame()
    else:
        units = expand_schedule(schedule_df, machine_df, acc_df)
        if units.empty:
            empty_banner_msg = (
                "⚠️ Could not expand schedule into units. Check that FG / Accessory SKUs "
                "match the catalog. You can edit catalogs in **📁 Data & Setup**."
            )

        # Track one schedule_load event per unique (mode, location, row-count)
        # signature within the session — keeps the log lean even when the user
        # toggles sliders that trigger reruns.
        try:
            _sig = (
                str(inputs.get("schedule_mode") or ""),
                str(inputs.get("location") or ""),
                int(len(schedule_df)),
            )
            if st.session_state.get("_schedule_load_sig") != _sig:
                _track_and_flush(
                    "schedule_load",
                    mode=inputs.get("schedule_mode"),
                    facility=inputs.get("location"),
                    rows=len(schedule_df),
                )
                st.session_state["_schedule_load_sig"] = _sig
        except Exception:
            pass

    if units.empty:
        capacity = pd.DataFrame()
        batt_sku = pd.DataFrame(columns=["fg_base", "batt_type", "units", "batt_per_unit",
                                           "total_batteries", "pct_of_total"])
        batt_type = pd.DataFrame(columns=["batt_type", "total_batteries", "units_count",
                                           "pct_of_total"])
    else:
        capacity = build_capacity_table(
            units, inputs["crew_config"], inputs["shift"], inputs["days"],
            inputs["safety"], inputs["efficiency"],
        )
        batt_sku = battery_demand_by_sku(units)
        batt_type = battery_demand_by_type(units)

    # Build the station × week heatmap input — uses the FULL schedule (not
    # the time-window slice) so the planner can spot weekly hotspots even
    # while looking at a sub-window. Returns None when there's no
    # PRODUCTION DAY data, in which case the Overview just skips the chart.
    weekly_util = _compute_weekly_utilization(
        schedule_df_full, machine_df, acc_df, inputs,
    )

    # Detect schedule month for display (Mon-YY format rows, not carryover)
    if schedule_df.empty or "CARRYOVER" not in schedule_df.columns:
        schedule_month = ""
    else:
        current_months = schedule_df.loc[~schedule_df["CARRYOVER"], "PRODUCTION MONTH"].unique().tolist()
        schedule_month = current_months[0] if current_months else ""

    # Top-of-page banner when there's no schedule loaded
    if empty_banner_msg:
        st.info(empty_banner_msg)

    # Top-of-page warning when the schedule references SKUs the catalogs don't
    # know about. `expand_schedule` quietly drops unknown FG rows and lets
    # unknown accessories build with zero acc labor — without this banner a
    # planner could trust analysis numbers that are missing units. Keep it
    # compact; the full list lives in 📁 Data & Setup → 🔍 Data Quality.
    # Auto-spread banner — fires when _auto_spread_dates filled in blank
    # PRODUCTION DAY rows. We surface it prominently so the planner knows
    # the dates driving the heatmap are *synthetic* (a level-load baseline),
    # not what their ERP told them.
    _auto = (
        schedule_df_full.attrs.get("auto_spread")
        if (hasattr(schedule_df_full, "attrs") and not schedule_df_full.empty)
        else None
    )
    if isinstance(_auto, dict) and _auto.get("count"):
        st.warning(
            "📅 **No PRODUCTION DAY in your schedule** — we auto-leveled "
            f"**{_auto['count']} of {_auto['total_rows']} units** across "
            f"**{_auto['target_month_label']}** "
            f"({_auto['working_days']} working days) so weekly labor is "
            "roughly balanced. The Overview heatmap reflects this "
            "synthetic sequence. To pin your own dates, add a "
            "**PRODUCTION DAY** column to your CSV (any date format works) "
            "and re-upload."
        )

    # Out-of-month warning — the Plan month picker is authoritative, so any
    # rows dated in a different month won't appear on the Calendar for the
    # selected month. Surface the count (we don't silently move their dates).
    _plan_ym = inputs.get("plan_month")
    if (
        _plan_ym is not None
        and not schedule_df_full.empty
        and "PRODUCTION DAY" in schedule_df_full.columns
    ):
        _pday = pd.to_datetime(schedule_df_full["PRODUCTION DAY"], errors="coerce")
        _out_mask = _pday.notna() & (
            (_pday.dt.year != _plan_ym[0]) | (_pday.dt.month != _plan_ym[1])
        )
        _n_out = int(_out_mask.sum())
        if _n_out:
            st.warning(
                f"📆 **{_n_out} row(s) are dated outside "
                f"{inputs.get('plan_month_label', 'the selected month')}** and "
                "won't appear on the Calendar for this month. Switch the "
                "**Plan month** in the sidebar to match your data, or re-date "
                "those rows in **📁 Data & Setup → 🗓 Sequence**."
            )

    _skipped_fg: list = []
    _unknown_acc: list = []
    if not units.empty and hasattr(units, "attrs"):
        _skipped_fg = list(units.attrs.get("skipped_fg", []) or [])
        _unknown_acc = list(units.attrs.get("unknown_acc", []) or [])
    if _skipped_fg or _unknown_acc:
        _preview_fg = ", ".join(f"`{s}`" for s in _skipped_fg[:5])
        _preview_acc = ", ".join(f"`{s}`" for s in _unknown_acc[:5])
        _bits = []
        if _skipped_fg:
            extra = f" (+{len(_skipped_fg) - 5} more)" if len(_skipped_fg) > 5 else ""
            _bits.append(
                f"**{len(_skipped_fg)} FG SKU(s) missing from machine catalog** — "
                f"their rows were dropped from analysis: {_preview_fg}{extra}."
            )
        if _unknown_acc:
            extra = f" (+{len(_unknown_acc) - 5} more)" if len(_unknown_acc) > 5 else ""
            _bits.append(
                f"**{len(_unknown_acc)} accessory SKU(s) missing from accessory catalog** — "
                f"those units built with zero accessory labor: {_preview_acc}{extra}."
            )
        st.warning(
            "⚠️ " + "  \n".join(_bits)
            + "  \n→ Quick-import them below, or edit catalogs in **📁 Data & Setup**."
        )

        # Quick import — one click adds every missing SKU to the catalogs as a
        # placeholder (zero labor, today's date) so its units stop being
        # dropped. The planner fills in real labor times afterwards.
        _n_missing = len(_skipped_fg) + len(_unknown_acc)
        # Defensive: st.secrets.get() raises if no secrets.toml exists at all
        # (e.g. local runs). Streamlit Cloud always has one, but guard anyway.
        try:
            _token = st.secrets.get("github_token", None)
        except Exception:
            _token = None
        _ic1, _ic2 = st.columns([2, 3])
        with _ic1:
            _do_import = st.button(
                f"➕ Import {_n_missing} missing SKU(s) into the catalogs",
                use_container_width=True,
                type="primary",
                disabled=not _token,
                help=(
                    "Adds each missing FG SKU to the machine catalog and each "
                    "missing accessory SKU to the accessory catalog as a "
                    "zero-labor placeholder, committed to GitHub. Fill in the "
                    "real labor times afterwards in 📁 Data & Setup."
                ),
            )
        if not _token:
            with _ic2:
                st.caption(
                    "🔒 Importing needs the GitHub token "
                    "(`github_token` in Streamlit Secrets)."
                )
        if _do_import:
            _added_all: list[str] = []
            _skipped_all: list[str] = []
            _failed = False
            try:
                with st.spinner("Importing missing SKUs into the catalogs…"):
                    if _skipped_fg:
                        _a, _s = _import_missing_skus_to_catalog(
                            "data/machine_clean.csv", "machine", _skipped_fg, _token,
                        )
                        _added_all += _a
                        _skipped_all += _s
                    if _unknown_acc:
                        _a, _s = _import_missing_skus_to_catalog(
                            "data/acc_clean.csv", "accessory", _unknown_acc, _token,
                        )
                        _added_all += _a
                        _skipped_all += _s
            except Exception as e:
                _failed = True
                st.error(f"❌ Import failed: {e}")
            if not _failed:
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                _track_and_flush(
                    "missing_sku_import",
                    n_added=len(_added_all), n_skipped=len(_skipped_all),
                )
                if _added_all:
                    st.success(
                        f"✅ Imported **{len(_added_all)}** SKU(s) as zero-labor "
                        "placeholders. They'll appear in the catalogs after the "
                        "app redeploys (~1 min) — then set their labor times in "
                        "**📁 Data & Setup**."
                        + (
                            f"  \n_Skipped {len(_skipped_all)} already-present / "
                            "invalid SKU(s)._" if _skipped_all else ""
                        )
                    )
                else:
                    st.info(
                        "Nothing imported — every missing SKU was already in the "
                        "catalog (another user may have just added them). Refresh "
                        "to see them."
                    )

    # Tabs — 6 top-level tabs, ordered from executive summary → planner detail → admin.
    # Batteries content folded into Capacity. Update Labor + Data Quality + Source Data
    # consolidated into the single 📁 Data & Setup tab.
    tabs = st.tabs([
        "🏠 Overview",
        "📆 Calendar",
        "📊 Capacity",
        "🔧 Recommendations",
        "⏱ Build Time",
        "🔀 Process Flow",
        "📁 Data & Setup",
    ])

    # Helper — show an "empty state" message inside analysis tabs when there's no schedule
    def _empty_state(tab_name: str):
        st.info(
            f"📭 **{tab_name} needs a schedule to populate.**  \n"
            "• In the sidebar, switch to **📤 Upload schedule file** or **✏️ Type a few SKUs**.  \n"
            "• You can still edit catalogs, items, packages, and the process flow from the "
            "**📁 Data & Setup** tab without a schedule."
        )

    with tabs[0]:
        if units.empty:
            _empty_state("Overview")
        else:
            tab_overview(units, capacity, batt_type, inputs, schedule_month, weekly_util=weekly_util)
    with tabs[1]:
        # Calendar — week & day rollups. ALWAYS use the full (unfiltered)
        # schedule here so the planner sees the entire month's per-week
        # rollup regardless of the sidebar Time-window setting. The helper
        # also handles the empty case with its own info banner.
        _render_calendar_view(
            schedule_df_full, machine_df, acc_df,
            time_window=inputs.get("time_window", "Whole month"),
            plan_month=inputs.get("plan_month"),
            plan_month_label=inputs.get("plan_month_label", ""),
        )
    with tabs[2]:
        if units.empty:
            _empty_state("Capacity")
        else:
            tab_capacity_vs_demand(capacity, inputs, batt_sku=batt_sku, batt_type=batt_type)
    with tabs[3]:
        if units.empty:
            _empty_state("Recommendations")
        else:
            tab_mitigation(capacity, batt_sku, units, inputs)
    with tabs[4]:
        if units.empty:
            _empty_state("Build Time")
        else:
            tab_cycle_time(units, inputs, machine_df, acc_df)
    with tabs[5]:
        # Process Flow can show the editable flow even without a schedule
        tab_process_flow(units, machine_df, acc_df, inputs)
    with tabs[6]:
        tab_data_setup(
            machine_df=machine_df, acc_df=acc_df, schedule_df=schedule_df,
            item_master_df=item_master_df, item_packages_df=item_packages_df,
            units=units, schedule_df_full=schedule_df_full,
        )

    # --- Footer ---------------------------------------------------------
    # Lightweight provenance + support line at the bottom of every page.
    st.markdown("---")
    st.caption(
        "Last updated: May 2026 · Questions or issues? Contact "
        "[tnguyen@anacorp.com](mailto:tnguyen@anacorp.com)"
    )


if __name__ == "__main__":
    main()
