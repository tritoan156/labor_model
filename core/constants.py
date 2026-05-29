"""Constants for the Labor Capacity Model (Henderson, Spartanburg, Cypress).

All business rules from the v17 model. Edit here, not in the UI.
"""

from datetime import datetime, date, timedelta

# ---------------------------------------------------------------------------
# Time helpers — Las Vegas / Pacific time
# ---------------------------------------------------------------------------
# Streamlit Cloud's containers run in UTC. If we call ``datetime.now()`` we
# get UTC wall-clock with no timezone info, which then renders as if it were
# Las Vegas time in the admin dashboard and on "Last Modified" cells — but
# is actually 7-8 hours ahead. Confusing for planners who glance at the log
# and see saves "from the future".
#
# ``now_local()`` returns a timezone-aware ``datetime`` in
# America/Los_Angeles. ``.isoformat()`` on the result automatically appends
# the offset (``-07:00`` for PDT, ``-08:00`` for PST), so timestamps are
# unambiguous in the log.
#
# zoneinfo first (stdlib, Python 3.9+); fall back to dateutil.tz on hosts
# that don't ship the OS TZ database (rare). Both are bundled with pandas
# transitively so this never adds a missing-dependency error in practice.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    LAS_VEGAS_TZ = _ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover — Windows without tzdata, etc.
    from dateutil import tz as _dateutil_tz
    LAS_VEGAS_TZ = _dateutil_tz.gettz("America/Los_Angeles")


def now_local() -> datetime:
    """Current wall-clock time in Las Vegas, as a tz-aware ``datetime``.

    Use this instead of ``datetime.now()`` anywhere a user-facing timestamp
    is generated (usage log, "saved_at" fields, "Last Modified" stamps).
    """
    return datetime.now(LAS_VEGAS_TZ)


def today_local_str() -> str:
    """Today's date in Las Vegas time as ``YYYY-MM-DD``.

    Used as the value for "Last Modified" cells when the user saves a
    catalog row.
    """
    return now_local().strftime("%Y-%m-%d")


def week_of_month(d) -> int:
    """1-based, Monday-anchored week index within ``d``'s own month.

    Weeks start Monday; week 1 is the calendar week that contains the 1st
    of the month, so the first and last weeks may be partial (Mon–Fri
    aligned on the shop floor). Accepts a ``date``, ``datetime``, or
    pandas ``Timestamp``.

    Examples (June 2026, the 1st is a Monday):
        Jun 1–5 → 1, Jun 8–12 → 2, … Jun 29–30 → 5.
    (May 2026, the 1st is a Friday): May 1 → 1, May 4–8 → 2, …
    """
    d = date(d.year, d.month, d.day)
    first = d.replace(day=1)
    first_monday = first - timedelta(days=first.weekday())
    d_monday = d - timedelta(days=d.weekday())
    return (d_monday - first_monday).days // 7 + 1


def week_of_month_series(s):
    """Apply :func:`week_of_month` over a datetime-like pandas Series and
    return a nullable ``Int64`` array (``pd.NA`` for NaT rows).

    Built as an explicit list → ``pd.array(dtype="Int64")`` rather than
    ``Series.apply(...).astype("Int64")``: applying a scalar function over a
    *datetime64* Series can make pandas retain the datetime dtype, which then
    fails to cast to Int64. Constructing the array directly avoids that.
    """
    import pandas as pd  # local import keeps this module import-light

    return pd.array(
        [week_of_month(d) if pd.notna(d) else pd.NA for d in s],
        dtype="Int64",
    )


# === Locations ===
LOCATIONS = ["Henderson", "Spartanburg", "Cypress"]

# 3-letter facility codes used as column suffixes in machine_clean.csv for
# per-facility station routing (e.g. "Final Station HND" / "Final Station
# SPB" / "Final Station CYP"). Keep this in sync with LOCATIONS — adding a
# new plant means adding both an entry here AND running the migration to
# add the matching CSV columns.
FACILITY_CODE = {
    "Henderson":   "HND",
    "Spartanburg": "SPB",
    "Cypress":     "CYP",
}

# === Working time defaults ===
DEFAULT_SHIFT_MINUTES = 480
DEFAULT_WORKING_DAYS = 20
DEFAULT_SAFETY_FACTOR = 1.00      # 1.00 = use 100% of capacity (no planning buffer)
DEFAULT_EFFICIENCY_FACTOR = 0.50  # 0.50 = 50% productive time; 1.00 = no loss

# === Utilization status thresholds ===
# Drive the color flag for each station (🟢 OK / 🟡 Tight / 🟠 Near cap / 🔴 Over).
# Tune these to match how aggressive your team wants the warnings to be.
UTIL_THRESHOLD_OVER = 1.00   # above this → 🔴 OVER capacity
UTIL_THRESHOLD_NEAR = 0.90   # above this → 🟠 Near capacity
UTIL_THRESHOLD_TIGHT = 0.75  # above this → 🟡 Tight

# === Final Assembly labor by unit class (person-mins) ===
# HS = PM only, Final = 30 (1 person × 30 min)
# HT = Head Trailer (Mount only) = 60 (2 ppl × 30 min)
# STD = Standard (Mount + Marry batched) = 60 + 96 = 156
FINAL_LABOR = {"HS": 30, "HT": 60, "STD": 156}

# Final Assembly crew override for HS units (uses 1 person, not 2)
HS_FINAL_CREW = 1

# === Battery defaults (when Acc data is missing) ===
DEFAULT_NAMEPLATE_PREP = 10  # PREP-NP (Nameplate Prep), person-mins per unit
DEFAULT_BATT_RAW = 320  # SUB-BTR, person-mins per battery
BATTERY_CYCLE_MINUTES = 170  # 10 prep + 160 build per battery (calendar mins per cell)

# === ETO labor ===
# Used for BOSS220 / BOSS400 units (separate ETO line)
ETO_LABOR_PER_UNIT = 960  # person-mins per ETO unit

# === Customer decal suffixes (collapse to base SKU) ===
CUSTOMER_SUFFIXES = ["HERC", "HRC", "UR", "ES"]

# === Station definitions ===
# Station name → (default HC, default Concurrent bays, default Crew per unit)
STATION_DEFAULTS = {
    "Warehouse (Pick)":   (2, 2, 1),
    "Wire Assembly":      (4, 4, 1),
    "Battery Assembly":   (8, 4, 2),
    "PM Acc (Headunit)":  (3, 3, 1),
    "Gen Accessories":    (8, 8, 1),
    "Com Accessories":    (1, 1, 1),
    "Trailer Assembly":   (6, 3, 2),
    "ETO":                (4, 2, 2),
    "Accessories KIT":    (1, 1, 1),
    "Final Assembly":     (4, 2, 2),
    "PDI":                (2, 2, 1),
    "QC":                 (4, 4, 1),
    "Ship":               (1, 1, 1),
}

# Internal station keys (used in code, not display)
STATION_KEYS = [
    "Warehouse", "Wire", "Battery", "PMAcc", "GenAcc", "ComAcc", "Trailer",
    "AccKIT", "Final", "PDI", "QC", "Ship", "ETO",
]

# === Production-area groupings (for the Capacity tab's per-area cap view) ===
# Groups stations into the physical/organizational areas planners think in, so
# the Capacity tab can show a binding unit cap per area. This lets a planner see
# what (e.g.) the Accessories or Trailer area can support on its own, separate
# from an easily-relievable bottleneck elsewhere (Shipping is often the binding
# station but is quick to add capacity to). Keys are display labels; values are
# lists of STATION_DEFAULTS display names. A station may appear in only one
# group; any station not listed here is bucketed under "Other" at render time.
STATION_AREA_GROUPS = {
    "Prep / Warehouse":   ["Warehouse (Pick)", "Wire Assembly"],
    "Battery":            ["Battery Assembly"],
    "Accessories":        ["PM Acc (Headunit)", "Gen Accessories",
                           "Com Accessories", "Accessories KIT"],
    "Trailer":            ["Trailer Assembly"],
    "ETO":                ["ETO"],
    "Final / PDI / QC":   ["Final Assembly", "PDI", "QC"],
    "Shipping":           ["Ship"],
}

# Map internal key → display name
STATION_KEY_TO_DISPLAY = {
    "Warehouse": "Warehouse (Pick)",
    "Wire": "Wire Assembly",
    "Battery": "Battery Assembly",
    "PMAcc": "PM Acc (Headunit)",
    "GenAcc": "Gen Accessories",
    "ComAcc": "Com Accessories",
    "Trailer": "Trailer Assembly",
    "AccKIT": "Accessories KIT",
    "Final": "Final Assembly",
    "PDI": "PDI",
    "QC": "QC",
    "Ship": "Ship",
    "ETO": "ETO",
}

# === Battery type assignments ===
# 4 battery types and the SKUs that use them.
# Rules from operations:
#   - 25-12: BOSS25 family (any model)
#   - 70-16: BOSS70-40 Hybrid (BOSS70-012, BOSS70-001)
#   - 70-20: BOSS70-65, BOSS70-45, BOSS70 PM
#   - 125-20: BOSS125, BOSS220 PM, BOSS400 PM
def get_battery_type(fg_base: str) -> str:
    """Return battery type ID for a given FG base SKU.

    Rules:
      BOSS25*           → 25-12  (1 batt/unit)
      BOSS70-012,-001   → 70-16  (1 batt/unit, BOSS70-40 Hybrid)
      Other BOSS70*     → 70-20  (1 batt/unit)
      BOSS125*          → 125-20 (1 batt/unit)
      BOSS220*          → 125-20 (3 batt/unit)
      BOSS400*          → 125-20 (5 batt/unit)
    """
    s = str(fg_base).upper().strip()
    if s.startswith("BOSS25"):
        return "25-12"
    if s.startswith("BOSS70"):
        # Compare the normalized value (upper+strip), not the raw fg_base, so
        # lower-case / whitespace-padded inputs (e.g. "boss70-012", "BOSS70-012 ")
        # still classify as the 70-16 Hybrid rather than falling through to 70-20.
        if s in ("BOSS70-012", "BOSS70-001"):
            return "70-16"
        return "70-20"
    if s.startswith("BOSS125") or s.startswith("BOSS220") or s.startswith("BOSS400"):
        return "125-20"
    return "UNKNOWN"

# Battery types in volume order (for prioritization in scheduler)
BATTERY_TYPES = ["25-12", "70-16", "70-20", "125-20"]

# Type volume rank for May 2026 (1=highest volume) — used for priority ordering.
# This may shift in future months; the scheduler can recompute dynamically.
TYPE_VOLUME_RANK_DEFAULT = {
    "70-20": 1,
    "125-20": 2,
    "70-16": 3,
    "25-12": 4,
}

# === Battery count overrides ===
# Some SKUs need more batteries than the Machine_Clean default.
# These take precedence if non-zero.
BATTERY_COUNT_OVERRIDES = {
    "BOSS220HS-002": 3,  # confirmed: 3 batteries (was 2)
    "BOSS400HS-002": 5,  # confirmed: 5 batteries
}

# === Family priority for sequencing within Final Assembly ===
# Higher-volume families first (carryover & ETO override this).
FAMILY_PRIORITY_DEFAULT = {
    "BOSS70-017": 1, "BOSS70-012": 2, "BOSS70HS-001": 3, "BOSS25-006": 4,
    "BOSS25-010": 5, "BOSS25-014": 6, "BOSS125-004": 7, "BOSS70-019": 8,
    "BOSS70-001": 9, "BOSS400HS-002": 10, "BOSS220HS-002": 11,
}

# === Marry batching rules (Final Assembly) ===
MARRY_BATCH_SIZE = 5
MARRY_RATE_FULL = 5  # 1 person can marry 5 same-family units/day
MARRY_RATE_ORPHAN = 4  # 1 person can marry 4 mixed-kW orphans/day
MARRY_PEOPLE = 2  # part of the 4 HC at Final Assembly
