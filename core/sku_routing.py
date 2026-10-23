"""Machine-SKU classification and per-facility routing seeds for new rows.

``data/machine_clean.csv`` carries 15 routing columns — ``Final Station`` /
``AccKIT Station`` / ``PDI Station`` / ``Wire Station`` / ``Undercarriage
Station``, each suffixed with a facility code from
:data:`core.constants.FACILITY_CODE` (``HND`` / ``SPB`` / ``CYP``). They are the
app's own overlay: never imported from the IE's Excel workbook, only set in the
catalog editor's dropdowns. A row added with those cells blank loads as the
default routing (the loader reads a blank as the station's own team), so it
never inherits a plant's custom routing — Henderson has no undercarriage crew
and sends PDS undercarriage work to Com Accessories; Cypress has no Wire team
and gives SDG wire work to Gen Accessories; and so on.

:class:`RoutingSeeder` fills those cells for a new SKU from the rows already in
the catalog, per facility column, by similarity:

1. rows of the **same family and class** (``BOSS25`` · ``Standard Trailer``,
   ``PDS`` · ``Standard`` …) — the most common value among them;
2. otherwise rows of the **same family**;
3. otherwise the **facility column's most common value** across the catalog;
4. otherwise (an empty catalog) the station's default.

Blank cells never vote: a group whose cells were never set falls through to
the next tier rather than "winning" with the default. A value the loader
would not accept (not a station key) counts as blank too. Ties go to the
value that appears first in the catalog, so a seed is deterministic.

The family / class classifiers live here because the seeding rule is defined
by them; ``app.py`` imports them under its old ``_machine_family`` /
``_machine_class`` names.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from .constants import FACILITY_CODE, STATION_KEYS

# ---------------------------------------------------------------------------
# SKU family / class
# ---------------------------------------------------------------------------
#: Order matters: longest prefixes first so BOSS220HS-002 resolves to BOSS220
#: (not the non-existent BOSS2 or BOSS22).
KNOWN_FAMILIES = ["BOSS400", "BOSS220", "BOSS125", "BOSS70", "BOSS25", "PDS", "SDG"]

#: What :func:`machine_class` returns for an ``XXX`` estimation placeholder.
PLACEHOLDER_CLASS = "⚠ Placeholder"


def machine_family(sku) -> str:
    """Map an FG SKU to a high-level product family (BOSS25 / BOSS70 / … /
    PDS / SDG); ``"OTHER"`` for anything unrecognised."""
    s = str(sku or "").upper().strip()
    if not s:
        return "OTHER"
    for fam in KNOWN_FAMILIES:
        if s.startswith(fam):
            return fam
    return "OTHER"


def machine_class(sku, description="") -> str:
    """Classify a machine SKU into one of:
    ⚠ Placeholder · Hybrid · Power Module · Head Trailer · Standard Trailer · Standard.

    PDS/SDG always classify as 'Standard' (compressor/generator base units).
    BOSS rules apply in this priority: Placeholder > Hybrid > Power Module >
    Head Trailer > Standard Trailer.
    """
    s = str(sku or "").upper().strip()
    d = str(description or "").upper()
    fam = machine_family(s)
    if "XXX" in s:
        return PLACEHOLDER_CLASS
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


# ---------------------------------------------------------------------------
# Routing columns
# ---------------------------------------------------------------------------
#: Station base of each routing column → the station it routes to by default
#: (what the loader reads a blank cell as), in the CSV's column order.
ROUTING_STATION_DEFAULTS: dict[str, str] = {
    "Final": "Final",
    "AccKIT": "AccKIT",
    "PDI": "PDI",
    "Wire": "Wire",
    "Undercarriage": "Undercarriage",
}

#: ``Final Station HND`` → station ``Final``, facility code ``HND``. Any code
#: of 2–4 capitals is accepted so a fourth plant's columns seed unchanged.
_ROUTING_COLUMN_RE = re.compile(
    r"^(Final|AccKIT|PDI|Wire|Undercarriage) Station ([A-Z]{2,4})$"
)


def routing_column(station: str, code: str) -> str:
    """``("PDI", "CYP")`` → ``"PDI Station CYP"``."""
    return f"{station} Station {code}"


def split_routing_column(column) -> tuple[str, str] | None:
    """``"PDI Station CYP"`` → ``("PDI", "CYP")``; ``None`` for any other column."""
    m = _ROUTING_COLUMN_RE.match(str(column or "").strip())
    return (m.group(1), m.group(2)) if m else None


def all_routing_columns(codes=None) -> list[str]:
    """The 15 per-facility routing columns in CSV order (station-major:
    ``Final Station HND``, ``Final Station SPB``, ``Final Station CYP``,
    ``AccKIT Station HND`` …)."""
    codes = list(codes) if codes is not None else list(FACILITY_CODE.values())
    return [routing_column(s, c) for s in ROUTING_STATION_DEFAULTS for c in codes]


def routing_columns_in(columns) -> list[str]:
    """The routing columns present in ``columns``, in their given order."""
    return [str(c) for c in columns if split_routing_column(c)]


def _facility_codes_in(columns) -> list[str]:
    """Facility codes the given routing columns cover, known plants first."""
    seen = {split_routing_column(c)[1] for c in columns if split_routing_column(c)}
    known = [c for c in FACILITY_CODE.values() if c in seen]
    return known + sorted(seen - set(known))


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------
TIER_GIVEN = "given"                 # the caller already had a value (e.g. the sheet)
TIER_FAMILY_CLASS = "family+class"
TIER_FAMILY = "family"
TIER_FACILITY = "facility"
TIER_DEFAULT = "default"

_VALID_STATIONS = frozenset(STATION_KEYS)


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def _clean_routing_value(value) -> str:
    """The cell as the loader would read it: a station key, or ``""`` for a
    blank / unknown value (which the loader turns into the default)."""
    if _is_blank(value):
        return ""
    text = str(value).strip()
    return text if text in _VALID_STATIONS else ""


def _majority(values) -> str | None:
    """Most common non-blank value; ties go to the earliest first appearance."""
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for i, v in enumerate(values):
        if not v:
            continue
        counts[v] = counts.get(v, 0) + 1
        first.setdefault(v, i)
    if not counts:
        return None
    return max(counts, key=lambda v: (counts[v], -first[v]))


@dataclass(frozen=True)
class RoutingSeed:
    """The routing a new SKU gets: ``values`` maps each routing column to a
    station key; ``tiers`` records which rule produced it."""
    sku: str
    family: str
    machine_class: str
    values: dict[str, str] = field(default_factory=dict)
    tiers: dict[str, str] = field(default_factory=dict)
    n_family_class: int = 0
    n_family: int = 0
    n_catalog: int = 0

    @property
    def codes(self) -> list[str]:
        return _facility_codes_in(self.values)

    def fill(self, row: dict) -> dict:
        """Write the seed into ``row`` — only into routing columns the row has
        and that are blank, so a value the caller already had (an Excel
        sheet's cell) always wins. Returns ``row`` for chaining."""
        for col, value in self.values.items():
            if col in row and _is_blank(row[col]):
                row[col] = value
        return row

    def with_given(self, row: dict) -> "RoutingSeed":
        """A copy where every non-blank routing cell in ``row`` replaces the
        seeded value (tier ``given``) — what :meth:`fill` would leave."""
        values = dict(self.values)
        tiers = dict(self.tiers)
        for col in self.values:
            if col in row and not _is_blank(row[col]):
                values[col] = str(row[col]).strip()
                tiers[col] = TIER_GIVEN
        return RoutingSeed(
            sku=self.sku, family=self.family, machine_class=self.machine_class,
            values=values, tiers=tiers, n_family_class=self.n_family_class,
            n_family=self.n_family, n_catalog=self.n_catalog,
        )

    def describe(self) -> str:
        """Where the values came from, for the planner: ``"9 PDS Standard
        row(s)"``, or ``"the sheet / 59 PDS row(s)"`` when stations came
        from more than one rule."""
        if not self.values:
            return "no routing columns"
        used = Counter(self.tiers.values())
        parts = []
        if TIER_GIVEN in used:
            parts.append("the sheet")
        if TIER_FAMILY_CLASS in used:
            parts.append(f"{self.n_family_class} {self.family} {self.machine_class} row(s)")
        if TIER_FAMILY in used:
            parts.append(f"{self.n_family} {self.family} row(s)")
        if TIER_FACILITY in used:
            why = (f"no {self.family} rows yet" if not self.n_family
                   else f"the {self.family} rows leave it blank")
            parts.append(f"the facility-wide majority ({why})")
        if TIER_DEFAULT in used:
            parts.append("the station defaults")
        return " / ".join(parts)

    def facility_summary(self, code: str) -> str:
        """The reroutes at one plant, compactly: ``"Final→Undercarriage ·
        AccKIT→ComAcc"``; ``"defaults"`` when every station keeps its own team."""
        parts = []
        for station, default in ROUTING_STATION_DEFAULTS.items():
            col = routing_column(station, code)
            value = self.values.get(col)
            if value and value != default:
                parts.append(f"{station}→{value}")
        return " · ".join(parts) if parts else "defaults"

    def preview_frame(self) -> pd.DataFrame:
        """Stations × facilities, one cell per routing column — the add-form
        preview."""
        codes = self.codes
        rows = []
        for station in ROUTING_STATION_DEFAULTS:
            cells = {code: self.values.get(routing_column(station, code), "") for code in codes}
            if any(cells.values()):
                rows.append({"Station": station, **cells})
        return pd.DataFrame(rows, columns=["Station", *codes])


class RoutingSeeder:
    """Seeds built from one catalog frame — build it once per save or
    preview, then :meth:`seed` every new SKU against it.

    ``catalog_df`` is the catalog as a frame: the raw CSV (``SKU`` column,
    blanks kept) or the loader's output (SKU index). ``columns`` limits
    which routing columns are seeded; by default every routing column the
    frame has — none for a frame without any (the accessory catalog), so the
    seeder is a no-op there.
    """

    def __init__(self, catalog_df: pd.DataFrame | None, columns=None):
        df = catalog_df if catalog_df is not None else pd.DataFrame()
        if columns is None:
            columns = routing_columns_in(df.columns)
        self.columns: list[str] = list(columns)
        self.n_catalog = int(len(df))
        if "SKU" in df.columns:
            skus = df["SKU"].tolist()
        else:
            skus = list(df.index) if len(df) else []
        descs = df["Description"].tolist() if "Description" in df.columns else [""] * len(skus)
        self._families = [machine_family(s) for s in skus]
        self._classes = [machine_class(s, d) for s, d in zip(skus, descs)]
        self._routing: dict[str, list[str]] = {}
        for col in self.columns:
            if col in df.columns:
                self._routing[col] = [_clean_routing_value(v) for v in df[col].tolist()]
            else:
                self._routing[col] = [""] * len(skus)

    def seed(self, sku, description="") -> RoutingSeed:
        family = machine_family(sku)
        klass = machine_class(sku, description)
        same_family = [i for i, f in enumerate(self._families) if f == family]
        same_both = [i for i in same_family if self._classes[i] == klass]
        values: dict[str, str] = {}
        tiers: dict[str, str] = {}
        for col in self.columns:
            cells = self._routing[col]
            station = split_routing_column(col)[0]
            default = ROUTING_STATION_DEFAULTS[station]
            for tier, idx in ((TIER_FAMILY_CLASS, same_both),
                              (TIER_FAMILY, same_family),
                              (TIER_FACILITY, range(len(cells)))):
                chosen = _majority(cells[i] for i in idx)
                if chosen:
                    values[col] = chosen
                    tiers[col] = tier
                    break
            else:
                values[col] = default
                tiers[col] = TIER_DEFAULT
        return RoutingSeed(
            sku=str(sku).strip(), family=family, machine_class=klass,
            values=values, tiers=tiers,
            n_family_class=len(same_both), n_family=len(same_family),
            n_catalog=self.n_catalog,
        )


def seed_facility_routing(catalog_df, sku, description="") -> RoutingSeed:
    """One-shot :class:`RoutingSeeder` for a single new SKU."""
    return RoutingSeeder(catalog_df).seed(sku, description)


def routing_seed_table(seeds) -> pd.DataFrame:
    """One row per seed for the multi-SKU previews: SKU, family, class, where
    the routing came from, and each plant's reroutes in short form."""
    seeds = list(seeds)
    codes: list[str] = []
    for s in seeds:
        for c in s.codes:
            if c not in codes:
                codes.append(c)
    rows = [
        {"SKU": s.sku, "Family": s.family, "Class": s.machine_class,
         "Seeded from": s.describe(),
         **{code: s.facility_summary(code) for code in codes}}
        for s in seeds
    ]
    return pd.DataFrame(rows, columns=["SKU", "Family", "Class", "Seeded from", *codes])
