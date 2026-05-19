"""Constants for the Labor Capacity Model (Henderson, Spartanburg, Cypress).

All business rules from the v17 model. Edit here, not in the UI.
"""

# === Locations ===
LOCATIONS = ["Henderson", "Spartanburg", "Cypress"]

# === Working time defaults ===
DEFAULT_SHIFT_MINUTES = 480
DEFAULT_WORKING_DAYS = 20
DEFAULT_SAFETY_FACTOR = 0.85

# === Final Assembly labor by unit class (person-mins) ===
# HS = PM only, Final = 30 (1 person × 30 min)
# HT = Head Trailer (Mount only) = 60 (2 ppl × 30 min)
# STD = Standard (Mount + Marry batched) = 60 + 96 = 156
FINAL_LABOR = {"HS": 30, "HT": 60, "STD": 156}

# Final Assembly crew override for HS units (uses 1 person, not 2)
HS_FINAL_CREW = 1

# === Battery defaults (when Acc data is missing) ===
DEFAULT_BATT_PREP = 10  # PREP-NP, person-mins per unit
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
    "Warehouse", "Wire", "Battery", "PMAcc", "GenAcc", "Trailer",
    "AccKIT", "Final", "PDI", "QC", "Ship", "ETO",
]

# Map internal key → display name
STATION_KEY_TO_DISPLAY = {
    "Warehouse": "Warehouse (Pick)",
    "Wire": "Wire Assembly",
    "Battery": "Battery Assembly",
    "PMAcc": "PM Acc (Headunit)",
    "GenAcc": "Gen Accessories",
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
        if fg_base in ("BOSS70-012", "BOSS70-001"):
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
