"""Tests for placeholder recovery in core.data_loader.

Covers the loader's ability to rescue planned units that arrive with a
BUILD QTY but no FG SKU ID by mapping their MODEL TYPE to a catalog `XXX`
estimation placeholder (and to warn about the ones it can't map).
"""
import pandas as pd
import pytest

from core.data_loader import (
    build_placeholder_map,
    load_schedule,
    _norm_model_key,
)

SCHEDULE_COLS = [
    "LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY",
    "PRODUCTION MONTH", "MODEL TYPE",
]


def _write_schedule(tmp_path, rows):
    """Write a minimal schedule CSV (latin-1, like a real ERP export) and
    return its path. `rows` is a list of dicts keyed by SCHEDULE_COLS."""
    df = pd.DataFrame(rows, columns=SCHEDULE_COLS)
    p = tmp_path / "sched.csv"
    df.to_csv(p, index=False, encoding="latin-1")
    return p


def _catalog():
    """A tiny machine catalog with one real SKU and two XXX placeholders."""
    return pd.DataFrame(
        [
            {"SKU": "BOSS70-001", "Description": "ENERGY BOSS 70 STD"},
            {"SKU": "BOSS25-25 XXX", "Description": "EBOSS 25-25 HYBRID ESTIMATION ASSEMBLY"},
            {"SKU": "BOSS70-65 XXX", "Description": "EBOSS 70-65 HYBRID ESTIMATION ASSEMBLY"},
        ]
    )


class TestNormModelKey:
    def test_collapses_eboss_prefix(self):
        # The schedule MODEL TYPE is inconsistent: "EBOSS25-25" vs "BOSS70-65".
        # Both must normalize to a BOSS-prefixed alphanumeric key.
        assert _norm_model_key("EBOSS25-25 Hybrid") == "BOSS2525HYBRID"
        assert _norm_model_key("BOSS70-65 Hybrid") == "BOSS7065HYBRID"

    def test_strips_non_alphanumerics_and_case(self):
        assert _norm_model_key("eboss 25-25  hybrid") == "BOSS2525HYBRID"

    def test_handles_blank_and_none(self):
        assert _norm_model_key("") == ""
        assert _norm_model_key(None) == "NONE"  # str(None) -> "NONE"; harmless


class TestBuildPlaceholderMap:
    def test_maps_description_to_sku(self):
        m = build_placeholder_map(_catalog())
        assert m["BOSS2525HYBRID"] == "BOSS25-25 XXX"
        assert m["BOSS7065HYBRID"] == "BOSS70-65 XXX"

    def test_excludes_non_placeholder_rows(self):
        m = build_placeholder_map(_catalog())
        # The real (non-XXX) SKU must NOT be in the map.
        assert "BOSS70001" not in m
        assert all(v.endswith("XXX") for v in m.values())

    def test_empty_or_none_catalog(self):
        assert build_placeholder_map(None) == {}
        assert build_placeholder_map(pd.DataFrame()) == {}


class TestPlaceholderRecovery:
    def _rows(self):
        return [
            # valid, has a real SKU
            {"LOCATION": "HENDERSON", "FG SKU ID": "BOSS70-001", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": "BOSS70 STD"},
            # SKU-less but MODEL TYPE matches a placeholder -> should be recovered
            {"LOCATION": "HENDERSON", "FG SKU ID": "", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": "EBOSS25-25 Hybrid"},
            {"LOCATION": "HENDERSON", "FG SKU ID": "", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": "EBOSS25-25 Hybrid"},
        ]

    def test_recovers_skuless_rows_with_map(self, tmp_path):
        p = _write_schedule(tmp_path, self._rows())
        pmap = build_placeholder_map(_catalog())
        df = load_schedule(p, location="HENDERSON", placeholder_map=pmap)
        # 1 real + 2 recovered = 3 units kept
        assert len(df) == 3
        assert (df["FG SKU ID"] == "BOSS25-25 XXX").sum() == 2
        rec = df.attrs["placeholder_recovered"]
        assert rec["count"] == 2
        assert rec["by_model"]["EBOSS25-25 Hybrid"] == ("BOSS25-25 XXX", 2)
        assert rec["unmatched"] == {}

    def test_no_map_drops_skuless_rows(self, tmp_path):
        # Backward compatibility: without a map the old behavior is preserved.
        p = _write_schedule(tmp_path, self._rows())
        df = load_schedule(p, location="HENDERSON")
        assert len(df) == 1  # only the row with a real SKU survives
        assert df.attrs["placeholder_recovered"]["count"] == 0

    def test_unmatched_model_is_recorded_not_recovered(self, tmp_path):
        rows = [
            {"LOCATION": "HENDERSON", "FG SKU ID": "", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": "SDG40S"},
        ]
        p = _write_schedule(tmp_path, rows)
        pmap = build_placeholder_map(_catalog())  # has no SDG40 placeholder
        df = load_schedule(p, location="HENDERSON", placeholder_map=pmap)
        assert len(df) == 0  # still dropped — no placeholder to use
        rec = df.attrs["placeholder_recovered"]
        assert rec["count"] == 0
        assert rec["unmatched"] == {"SDG40S": 1}

    def test_blank_model_type_is_skipped(self, tmp_path):
        # A SKU-less row with no MODEL TYPE can't be matched and shouldn't be
        # counted as "unmatched" noise — it's just dropped.
        rows = [
            {"LOCATION": "HENDERSON", "FG SKU ID": "", "FG ACCRY SKU ID": "",
             "BUILD QTY": "1", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": ""},
        ]
        p = _write_schedule(tmp_path, rows)
        pmap = build_placeholder_map(_catalog())
        df = load_schedule(p, location="HENDERSON", placeholder_map=pmap)
        rec = df.attrs["placeholder_recovered"]
        assert rec["count"] == 0
        assert rec["by_model"] == {}
        assert rec["unmatched"] == {}

    def test_zero_qty_skuless_row_is_not_recovered(self, tmp_path):
        # A 0-qty phantom must never be resurrected by recovery.
        rows = [
            {"LOCATION": "HENDERSON", "FG SKU ID": "", "FG ACCRY SKU ID": "",
             "BUILD QTY": "0", "PRODUCTION MONTH": "Jul-26", "MODEL TYPE": "EBOSS25-25 Hybrid"},
        ]
        p = _write_schedule(tmp_path, rows)
        pmap = build_placeholder_map(_catalog())
        df = load_schedule(p, location="HENDERSON", placeholder_map=pmap)
        assert len(df) == 0
        assert df.attrs["placeholder_recovered"]["count"] == 0
