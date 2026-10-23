"""core.sku_routing — per-facility routing seeds for new machine SKUs.

A SKU added through any of the app's paths used to leave the 15 routing
columns blank, so it loaded as the default team at every plant and never
inherited a plant's custom routing (Henderson: PDS undercarriage work goes to
Com Accessories; Cypress: SDG wire work goes to Gen Accessories). The seeder
copies the routing of the most similar catalog row — same family and class,
then same family, then the facility column's majority — and falls back to the
station default only when the catalog is empty. Blank cells never vote.

These tests pin that rule on small synthetic catalogs, then check it against
the real ``data/machine_clean.csv`` so a data change that would make a seed
wrong shows up here rather than in a planner's capacity numbers.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.constants import FACILITY_CODE, STATION_KEYS
from core.data_loader import load_machine_labor
from core.excel_io import ROUTING_OPTIONS
from core.sku_routing import (
    KNOWN_FAMILIES, PLACEHOLDER_CLASS, ROUTING_STATION_DEFAULTS,
    TIER_DEFAULT, TIER_FACILITY, TIER_FAMILY, TIER_FAMILY_CLASS, TIER_GIVEN,
    RoutingSeed, RoutingSeeder,
    all_routing_columns, machine_class, machine_family, routing_column,
    routing_columns_in, routing_seed_table, seed_facility_routing,
    split_routing_column,
)

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "machine_clean.csv"
COLS = all_routing_columns()


def _row(sku, desc="", **routing):
    r = {"SKU": sku, "Description": desc, "Warehouse": "10"}
    r.update({c: "" for c in COLS})
    r.update(routing)
    return r


def _catalog(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=["SKU", "Description", "Warehouse", *COLS])


def _real_catalog() -> pd.DataFrame:
    # Text cells, blanks kept — what the save paths fetch from GitHub.
    return pd.read_csv(CSV, dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# The classifiers (moved here from app.py; the seeder keys on them)
# ---------------------------------------------------------------------------
class TestClassifiers:
    @pytest.mark.parametrize("sku, family", [
        ("BOSS220HS-002", "BOSS220"),   # longest prefix wins, not BOSS2/BOSS22
        ("boss25-010", "BOSS25"),
        ("PDS185L-5E1", "PDS"),
        ("SDG45-A004", "SDG"),
        ("10225-GW", "OTHER"),
        ("", "OTHER"),
        (None, "OTHER"),
    ])
    def test_family(self, sku, family):
        assert machine_family(sku) == family

    @pytest.mark.parametrize("sku, desc, klass", [
        ("BOSS25-25 XXX", "", PLACEHOLDER_CLASS),
        ("PDS185-001", "PDS 185 SKID", "Standard"),      # PDS/SDG never read the description
        ("SDG45-001", "", "Standard"),
        ("BOSS70-012", "EBOSS 70 HYBRID TRAILER", "Hybrid"),
        ("BOSS220HS-001", "", "Power Module"),
        ("BOSS25-001", "EBOSS 25 PM SKID", "Power Module"),
        ("BOSS125HT-001", "", "Head Trailer"),
        ("BOSS25-010", "EBOSS 25 TRAILER", "Standard Trailer"),
    ])
    def test_class(self, sku, desc, klass):
        assert machine_class(sku, desc) == klass

    def test_app_keeps_its_names_bound_to_these(self):
        app = pytest.importorskip("app")
        assert app._machine_family is machine_family
        assert app._machine_class is machine_class
        assert app.KNOWN_FAMILIES is KNOWN_FAMILIES


# ---------------------------------------------------------------------------
# The routing columns
# ---------------------------------------------------------------------------
class TestRoutingColumns:
    def test_fifteen_columns_in_the_csv_order(self):
        header = list(pd.read_csv(CSV, nrows=0).columns)
        in_csv = [c for c in header if split_routing_column(c)]
        assert COLS == in_csv and len(COLS) == 15
        assert COLS[:3] == ["Final Station HND", "Final Station SPB", "Final Station CYP"]

    def test_split_and_build(self):
        assert split_routing_column("PDI Station CYP") == ("PDI", "CYP")
        assert split_routing_column("Undercarriage Station PHX") == ("Undercarriage", "PHX")
        assert split_routing_column("Final Station") is None
        assert split_routing_column("FN_Assy") is None
        assert routing_column("Wire", "SPB") == "Wire Station SPB"

    def test_routing_columns_in_keeps_order_and_skips_the_rest(self):
        assert routing_columns_in(["SKU", "Wire Station CYP", "Wire", "Final Station HND"]) == [
            "Wire Station CYP", "Final Station HND",
        ]

    def test_defaults_are_the_loader_defaults(self):
        # The loader reads a blank cell as the station's own key.
        assert ROUTING_STATION_DEFAULTS == {
            "Final": "Final", "AccKIT": "AccKIT", "PDI": "PDI",
            "Wire": "Wire", "Undercarriage": "Undercarriage",
        }
        assert set(ROUTING_STATION_DEFAULTS.values()) <= set(STATION_KEYS)


# ---------------------------------------------------------------------------
# The seeding rule, tier by tier
# ---------------------------------------------------------------------------
class TestSeedTiers:
    def test_same_family_and_class_wins_over_the_rest_of_the_family(self):
        cat = _catalog(
            _row("PDS100-001", **{"Final Station HND": "Undercarriage", "AccKIT Station HND": "ComAcc"}),
            _row("PDS100-002", **{"Final Station HND": "Undercarriage", "AccKIT Station HND": "ComAcc"}),
            _row("PDS100 XXX", **{"Final Station HND": "Final", "AccKIT Station HND": "GenAcc"}),
        )
        standard = seed_facility_routing(cat, "PDS999-001")
        assert standard.family == "PDS" and standard.machine_class == "Standard"
        assert standard.values["Final Station HND"] == "Undercarriage"
        assert standard.values["AccKIT Station HND"] == "ComAcc"
        assert standard.tiers["Final Station HND"] == TIER_FAMILY_CLASS
        assert standard.n_family_class == 2 and standard.n_family == 3

        placeholder = seed_facility_routing(cat, "PDS400S XXX")
        assert placeholder.machine_class == PLACEHOLDER_CLASS
        assert placeholder.values["Final Station HND"] == "Final"
        assert placeholder.values["AccKIT Station HND"] == "GenAcc"
        assert placeholder.n_family_class == 1

    def test_family_only_when_no_row_shares_the_class(self):
        cat = _catalog(
            _row("BOSS25-010", "EBOSS 25 TRAILER", **{"AccKIT Station HND": "GenAcc"}),
            _row("BOSS25-011", "EBOSS 25 TRAILER", **{"AccKIT Station HND": "GenAcc"}),
            _row("SDG45-001", **{"AccKIT Station HND": "ComAcc"}),
        )
        seed = seed_facility_routing(cat, "BOSS25HS-001")   # Power Module: no such BOSS25 row
        assert seed.machine_class == "Power Module" and seed.n_family_class == 0
        assert seed.values["AccKIT Station HND"] == "GenAcc"
        assert seed.tiers["AccKIT Station HND"] == TIER_FAMILY
        assert "2 BOSS25 row(s)" in seed.describe()

    def test_facility_majority_when_the_family_is_new(self):
        cat = _catalog(
            _row("BOSS25-010", **{"AccKIT Station CYP": "GenAcc"}),
            _row("PDS100-001", **{"AccKIT Station CYP": "ComAcc"}),
            _row("SDG45-001", **{"AccKIT Station CYP": "GenAcc"}),
        )
        seed = seed_facility_routing(cat, "BOSS400-001")
        assert seed.n_family == 0
        assert seed.values["AccKIT Station CYP"] == "GenAcc"
        assert seed.tiers["AccKIT Station CYP"] == TIER_FACILITY
        assert "no BOSS400 rows yet" in seed.describe()

    def test_empty_catalog_falls_back_to_the_station_defaults(self):
        seed = seed_facility_routing(_catalog(), "PDS999-001")
        assert seed.n_catalog == 0
        assert len(seed.values) == 15
        assert all(v == ROUTING_STATION_DEFAULTS[split_routing_column(c)[0]]
                   for c, v in seed.values.items())
        assert set(seed.tiers.values()) == {TIER_DEFAULT}
        assert seed.describe() == "the station defaults"
        assert seed.facility_summary("HND") == "defaults"

    def test_blank_cells_never_vote(self):
        # Every PDS Standard row leaves PDI CYP blank → that column falls
        # through to the family (a placeholder that did set it), not to the
        # default PDI that the blanks would "win" with.
        cat = _catalog(
            _row("PDS100-001", **{"Final Station HND": "Undercarriage"}),
            _row("PDS100-002", **{"Final Station HND": "Undercarriage"}),
            _row("PDS100-003", **{"Final Station HND": "Undercarriage"}),
            _row("PDS100 XXX", **{"PDI Station CYP": "ComAcc"}),
        )
        seed = seed_facility_routing(cat, "PDS999-001")
        assert seed.values["PDI Station CYP"] == "ComAcc"
        assert seed.tiers["PDI Station CYP"] == TIER_FAMILY
        assert seed.tiers["Final Station HND"] == TIER_FAMILY_CLASS
        # Nothing anywhere for Wire SPB → default, and describe() says so.
        assert seed.values["Wire Station SPB"] == "Wire"
        assert seed.tiers["Wire Station SPB"] == TIER_DEFAULT
        assert "3 PDS Standard row(s)" in seed.describe()
        assert "the station defaults" in seed.describe()

    def test_values_the_loader_would_reject_count_as_blank(self):
        cat = _catalog(
            _row("SDG45-001", **{"AccKIT Station HND": "genacc"}),   # wrong case
            _row("SDG45-002", **{"AccKIT Station HND": "Nope"}),
            _row("SDG45-003", **{"AccKIT Station HND": "GenAcc"}),
        )
        seed = seed_facility_routing(cat, "SDG999-001")
        assert seed.values["AccKIT Station HND"] == "GenAcc"
        assert all(v in STATION_KEYS for v in seed.values.values())

    def test_nan_cells_are_blank(self):
        # A frame straight from pd.read_csv carries NaN, not "".
        cat = _catalog(_row("SDG45-001", **{"AccKIT Station HND": "GenAcc"}))
        cat.loc[0, "PDI Station HND"] = float("nan")
        seed = seed_facility_routing(cat, "SDG999-001")
        assert seed.values["PDI Station HND"] == "PDI"
        assert seed.values["AccKIT Station HND"] == "GenAcc"

    def test_ties_go_to_the_value_seen_first(self):
        cat = _catalog(
            _row("SDG45-001", **{"PDI Station HND": "GenAcc"}),
            _row("SDG45-002", **{"PDI Station HND": "PDI"}),
        )
        assert seed_facility_routing(cat, "SDG999-001").values["PDI Station HND"] == "GenAcc"
        flipped = _catalog(
            _row("SDG45-002", **{"PDI Station HND": "PDI"}),
            _row("SDG45-001", **{"PDI Station HND": "GenAcc"}),
        )
        assert seed_facility_routing(flipped, "SDG999-001").values["PDI Station HND"] == "PDI"

    def test_only_the_frames_routing_columns_are_seeded(self):
        cat = pd.DataFrame([{"SKU": "PDS100-001", "Final Station HND": "Undercarriage"}])
        seed = seed_facility_routing(cat, "PDS999-001")
        assert seed.values == {"Final Station HND": "Undercarriage"}
        assert seed.codes == ["HND"]

    def test_a_frame_without_routing_columns_is_a_no_op(self):
        acc = pd.DataFrame([{"SKU": "BOSS25-A001", "AccKIT": 10}])
        seed = seed_facility_routing(acc, "BOSS25 AXXX")
        assert seed.values == {} and seed.describe() == "no routing columns"
        assert seed.preview_frame().empty
        assert seed_facility_routing(None, "X").values == {}
        assert seed_facility_routing(pd.DataFrame(), "X").values == {}

    def test_the_loaders_frame_seeds_too(self, tmp_path):
        # load_machine_labor() output: SKU is the index, defaults filled in.
        src = _catalog(
            _row("PDS100-001", "PDS", **{"Final Station HND": "Undercarriage"}),
            _row("PDS100-002", "PDS", **{"Final Station HND": "Undercarriage"}),
        )
        p = tmp_path / "m.csv"
        src.to_csv(p, index=False)
        loaded = load_machine_labor(p)
        seed = RoutingSeeder(loaded).seed("PDS999-001")
        assert seed.values["Final Station HND"] == "Undercarriage"
        assert seed.n_family == 2

    def test_a_fourth_plants_columns_seed_unchanged(self):
        cols = all_routing_columns(["HND", "PHX"])
        cat = pd.DataFrame([{"SKU": "PDS100-001", **{c: "ComAcc" for c in cols}}])
        seed = seed_facility_routing(cat, "PDS999-001")
        assert seed.codes == ["HND", "PHX"]
        assert seed.values["AccKIT Station PHX"] == "ComAcc"
        assert list(seed.preview_frame().columns) == ["Station", "HND", "PHX"]


class TestSeedIntoARow:
    CAT = _catalog(
        _row("PDS100-001", **{"Final Station HND": "Undercarriage", "AccKIT Station HND": "ComAcc"}),
    )

    def test_fill_writes_only_blank_routing_cells_the_row_has(self):
        seed = seed_facility_routing(self.CAT, "PDS999-001")
        row = {"SKU": "PDS999-001", "Warehouse": "", "Final Station HND": "Final",
               "AccKIT Station HND": "", "PDI Station HND": None}
        out = seed.fill(row)
        assert out is row
        assert row["Final Station HND"] == "Final"            # given → kept
        assert row["AccKIT Station HND"] == "ComAcc"          # blank → seeded
        assert row["PDI Station HND"] == "PDI"                # None → seeded
        assert row["Warehouse"] == ""                         # not a routing cell
        assert "Wire Station CYP" not in row                  # column the row lacks

    def test_with_given_reports_the_sheets_values(self):
        seed = seed_facility_routing(self.CAT, "PDS999-001")
        shown = seed.with_given({"SKU": "PDS999-001", "Final Station HND": " Final ", "PDI Station HND": ""})
        assert shown.values["Final Station HND"] == "Final"
        assert shown.tiers["Final Station HND"] == TIER_GIVEN
        assert shown.values["AccKIT Station HND"] == "ComAcc"
        assert shown.describe().startswith("the sheet / ")
        assert seed.values["Final Station HND"] == "Undercarriage"   # original untouched

    def test_facility_summary_lists_only_the_reroutes(self):
        seed = seed_facility_routing(self.CAT, "PDS999-001")
        assert seed.facility_summary("HND") == "Final→Undercarriage · AccKIT→ComAcc"
        assert seed.facility_summary("SPB") == "defaults"

    def test_preview_frame_is_stations_by_facility(self):
        frame = seed_facility_routing(self.CAT, "PDS999-001").preview_frame()
        assert list(frame.columns) == ["Station", *FACILITY_CODE.values()]
        assert list(frame["Station"]) == list(ROUTING_STATION_DEFAULTS)
        assert frame.set_index("Station").at["Final", "HND"] == "Undercarriage"
        assert frame.set_index("Station").at["Final", "CYP"] == "Final"

    def test_seed_table_has_one_line_per_sku(self):
        cat = _catalog(
            _row("PDS100-001", **{"Final Station HND": "Undercarriage", "AccKIT Station HND": "ComAcc"}),
            _row("SDG45-001", **{"Final Station HND": "Final", "AccKIT Station HND": "GenAcc"}),
            _row("BOSS25-010", **{"Final Station HND": "Final", "AccKIT Station HND": "AccKIT"}),
        )
        seeds = [seed_facility_routing(cat, s) for s in ("PDS999-001", "SDG999-001", "BOSS25-011")]
        table = routing_seed_table(seeds)
        assert list(table.columns) == ["SKU", "Family", "Class", "Seeded from", "HND", "SPB", "CYP"]
        assert list(table["SKU"]) == ["PDS999-001", "SDG999-001", "BOSS25-011"]
        assert table.at[0, "HND"] == "Final→Undercarriage · AccKIT→ComAcc"
        assert table.at[1, "HND"] == "AccKIT→GenAcc"
        assert table.at[2, "HND"] == "defaults"
        assert table.at[0, "Seeded from"].startswith("1 PDS Standard row(s)")
        assert routing_seed_table([]).empty

    def test_seed_is_immutable(self):
        seed = seed_facility_routing(self.CAT, "PDS999-001")
        with pytest.raises(Exception):
            seed.sku = "other"
        assert isinstance(seed, RoutingSeed)


# ---------------------------------------------------------------------------
# Against the real catalog — the plants' custom routing must carry over
# ---------------------------------------------------------------------------
class TestAgainstTheRealCatalog:
    @pytest.fixture(scope="class")
    def seeder(self):
        return RoutingSeeder(_real_catalog())

    def test_the_real_catalog_has_the_fifteen_columns_and_blanks(self, seeder):
        assert seeder.columns == COLS
        raw = _real_catalog()
        assert (raw[COLS] == "").any().any(), "the blank-never-votes rule needs real blanks to matter"

    def test_a_new_pds_compressor_inherits_hendersons_undercarriage_reroute(self, seeder):
        seed = seeder.seed("PDS999-001")
        assert seed.tiers["Final Station HND"] == TIER_FAMILY_CLASS
        assert seed.values["Final Station HND"] == "Undercarriage"
        assert seed.values["AccKIT Station HND"] == "ComAcc"
        assert seed.values["PDI Station HND"] == "ComAcc"
        # Henderson has no undercarriage crew → second hop to Com Accessories;
        # the other plants keep their Undercarriage team.
        assert seed.values["Undercarriage Station HND"] == "ComAcc"
        assert seed.values["Undercarriage Station SPB"] == "Undercarriage"
        assert seed.values["Undercarriage Station CYP"] == "Undercarriage"
        # Cypress has no Wire team for PDS work.
        assert seed.values["Wire Station CYP"] == "ComAcc"
        assert seed.values["Wire Station HND"] == "Wire"

    def test_a_new_sdg_generator_inherits_the_genacc_routing(self, seeder):
        seed = seeder.seed("SDG999-001")
        assert seed.values["AccKIT Station HND"] == "GenAcc"
        assert seed.values["AccKIT Station CYP"] == "GenAcc"
        assert seed.values["PDI Station CYP"] == "GenAcc"
        assert seed.values["Wire Station CYP"] == "GenAcc"
        assert seed.values["Final Station HND"] == "Final"

    def test_a_new_pds_placeholder_follows_the_pds_placeholders(self, seeder):
        seed = seeder.seed("PDS400S XXX")
        assert seed.machine_class == PLACEHOLDER_CLASS
        assert seed.values["Final Station HND"] == "Undercarriage"
        assert seed.values["Undercarriage Station HND"] == "ComAcc"

    @pytest.mark.parametrize("sku, desc", [
        ("BOSS400-001", "ENERGY BOSS 400 TRAILER"),
        ("BOSS220-009", ""),
        ("BOSS125HS-009", ""),
        ("BOSS70-099", "EBOSS 70 HYBRID"),
        ("BOSS25HT-009", ""),
        ("PDS999-001", ""),
        ("SDG999-001", ""),
        ("10999-ZZ", "SOMETHING UNRECOGNISED"),
        ("BOSS25-25 XXX", ""),
    ])
    def test_every_kind_of_new_sku_gets_fifteen_dropdown_values(self, seeder, sku, desc):
        seed = seeder.seed(sku, desc)
        assert set(seed.values) == set(COLS)
        for col, value in seed.values.items():
            station, _code = split_routing_column(col)
            assert value, f"{sku}: {col} seeded blank"
            assert value in ROUTING_OPTIONS[station], (
                f"{sku}: {col} = {value!r} isn't a value the editor's dropdown offers"
            )
        assert TIER_DEFAULT not in seed.tiers.values(), "the real catalog covers every column"
