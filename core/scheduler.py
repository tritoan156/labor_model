"""Battery scheduler for the Henderson model.

Given a set of finished units (each with a battery type and battery count),
schedule the batteries across N flexible cells subject to:

  1. Priority: carryover → ETO → highest-volume type → FIFO
  2. Single + 2-batt units: all batteries on one cell (no parallel)
  3. 3+ batt units: spread across cells in parallel
  4. Minimize type changeovers (sticky 1-day threshold)
  5. Cell daily budget = shift_minutes (480 by default)

Returns a flat schedule (one row per battery slot) plus aggregations.
"""
from collections import defaultdict
from typing import Optional

import pandas as pd

from .constants import (
    BATTERY_CYCLE_MINUTES, DEFAULT_SHIFT_MINUTES, TYPE_VOLUME_RANK_DEFAULT,
)


def _priority_key(item: dict, type_rank: dict[str, int]) -> tuple:
    """Sort key for battery priority: carryover, ETO, type-volume, unit_id, batt_idx."""
    return (
        not item["carryover"],   # False sorts before True → carryover first
        not item["eto"],         # ETO next
        type_rank.get(item["batt_type"], 99),
        item["unit_id"],
        item["batt_idx_in_unit"],
    )


def build_battery_items(units_df: pd.DataFrame) -> list[dict]:
    """Expand units_df into one row per battery (each unit's bat count rows).

    Returns list of dicts (not a DataFrame) for cheap iteration during scheduling.
    """
    items = []
    for _, u in units_df.iterrows():
        if u["Battery"] == 0:
            continue
        bat_count = int(u["Bat"]) if u["Bat"] else 1
        is_eto = "BOSS220" in str(u["fg_base"]).upper() or "BOSS400" in str(u["fg_base"]).upper()
        for batt_idx in range(1, bat_count + 1):
            items.append({
                "unit_id": int(u["unit_id"]),
                "fg_base": u["fg_base"],
                "fg_raw": u["fg_raw"],
                "decal": u.get("decal", ""),
                "carryover": bool(u["carryover"]),
                "eto": is_eto,
                "batt_type": u["batt_type"],
                "batt_idx_in_unit": batt_idx,
                "batt_count_total": bat_count,
                "unit_label": f"{u['fg_base']}-#{int(u['unit_id']):03d}",
            })
    return items


def schedule_batteries(units_df: pd.DataFrame,
                        n_cells: int = 4,
                        shift_minutes: int = DEFAULT_SHIFT_MINUTES,
                        cycle_minutes: int = BATTERY_CYCLE_MINUTES,
                        type_rank: Optional[dict[str, int]] = None,
                        sticky_threshold_min: Optional[float] = None) -> pd.DataFrame:
    """Schedule batteries across cells. Returns flat per-battery schedule.

    Args:
      units_df: Output of expand_schedule()
      n_cells: number of battery cells (default 4)
      shift_minutes: cell daily budget (default 480)
      cycle_minutes: time per battery (default 170 = 10 prep + 160 build)
      type_rank: dict mapping battery type → priority (lower = higher priority).
                 Defaults to volume-based ranking.
      sticky_threshold_min: max minute lead a same-type cell can have over min-load
                 cell before we switch. Default = 1 shift (480 min).

    Returns DataFrame columns:
      cell_id, day, start_min, end_min, unit_id, unit_label, fg_base, batt_type,
      batt_idx, batt_total, carryover, eto
    """
    if type_rank is None:
        type_rank = TYPE_VOLUME_RANK_DEFAULT
    if sticky_threshold_min is None:
        sticky_threshold_min = shift_minutes

    items = build_battery_items(units_df)
    items.sort(key=lambda x: _priority_key(x, type_rank))

    # Initialise cells
    cells = [
        {"id": i + 1, "minutes_used": 0.0, "current_type": None, "schedule": []}
        for i in range(n_cells)
    ]

    def _cell_at(cell):
        total = cell["minutes_used"]
        day = int(total // shift_minutes) + 1
        minute = total % shift_minutes
        return day, minute

    def _assign(cell, batt):
        day, start = _cell_at(cell)
        # If battery wouldn't fit in remainder of today, push to start of tomorrow
        if start + cycle_minutes > shift_minutes:
            cell["minutes_used"] = day * shift_minutes
            day, start = _cell_at(cell)
        cell["schedule"].append({
            "cell_id": cell["id"],
            "day": day,
            "start_min": start,
            "end_min": start + cycle_minutes,
            "unit_id": batt["unit_id"],
            "unit_label": batt["unit_label"],
            "fg_base": batt["fg_base"],
            "batt_type": batt["batt_type"],
            "batt_idx": batt["batt_idx_in_unit"],
            "batt_total": batt["batt_count_total"],
            "carryover": batt["carryover"],
            "eto": batt["eto"],
        })
        cell["minutes_used"] += cycle_minutes
        cell["current_type"] = batt["batt_type"]

    def _pick_cell(batt):
        """Balanced cell picker — prefers same-type cell, but only if not too far ahead."""
        min_load = min(c["minutes_used"] for c in cells)
        same_type = [c for c in cells if c["current_type"] == batt["batt_type"]]
        if same_type:
            best = min(same_type, key=lambda c: c["minutes_used"])
            if best["minutes_used"] - min_load <= sticky_threshold_min:
                return best
        return min(cells, key=lambda c: c["minutes_used"])

    # Group items by unit (so multi-batt units are scheduled together)
    by_unit = defaultdict(list)
    unit_order = []
    for it in items:
        if it["unit_label"] not in by_unit:
            unit_order.append(it["unit_label"])
        by_unit[it["unit_label"]].append(it)

    # Schedule each unit
    for unit_label in unit_order:
        batts = by_unit[unit_label]
        bat_total = batts[0]["batt_count_total"]

        if bat_total <= 2:
            # All on one cell
            cell = _pick_cell(batts[0])
            for b in batts:
                _assign(cell, b)
        else:
            # 3+: parallel across least-loaded cells
            n_par = min(bat_total, n_cells)
            chosen = sorted(cells, key=lambda c: c["minutes_used"])[:n_par]
            for i, b in enumerate(batts):
                _assign(chosen[i % n_par], b)

    # Flatten to DataFrame
    rows = []
    for c in cells:
        for s in c["schedule"]:
            rows.append(s)
    sched = pd.DataFrame(rows)
    sched = sched.sort_values(by=["day", "cell_id", "start_min"]).reset_index(drop=True)
    return sched


def daily_cell_view(schedule: pd.DataFrame, n_cells: int = 4) -> pd.DataFrame:
    """Pivot schedule into Day × Cell grid. Each cell shows compact type counts.

    Returns DataFrame indexed by Day with one column per cell.
    """
    if schedule.empty:
        return pd.DataFrame()
    max_day = int(schedule["day"].max())
    rows = []
    for day in range(1, max_day + 1):
        row = {"Day": f"D{day}"}
        for cell_id in range(1, n_cells + 1):
            items = schedule[(schedule["day"] == day) & (schedule["cell_id"] == cell_id)]
            if items.empty:
                row[f"Cell {cell_id}"] = "— idle —"
            else:
                tc = items.groupby("batt_type").size().to_dict()
                row[f"Cell {cell_id}"] = ", ".join(f"{t}×{n}" for t, n in tc.items())
        rows.append(row)
    return pd.DataFrame(rows).set_index("Day")


def unit_completion_view(schedule: pd.DataFrame) -> pd.DataFrame:
    """Per-finished-unit view: when does each unit have ALL its batteries done?

    Returns DataFrame with: unit_label, fg_base, batt_type, batt_total,
    completion_day, cells_used.
    """
    if schedule.empty:
        return pd.DataFrame()
    g = schedule.groupby("unit_label").agg(
        unit_id=("unit_id", "first"),
        fg_base=("fg_base", "first"),
        batt_type=("batt_type", "first"),
        batt_total=("batt_total", "first"),
        completion_day=("day", "max"),
        first_day=("day", "min"),
        cells_used=("cell_id", lambda x: ",".join(str(c) for c in sorted(x.unique()))),
    ).reset_index()
    g = g.sort_values(by=["completion_day", "unit_id"]).reset_index(drop=True)
    return g
