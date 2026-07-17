"""Tests for per-facility settings, focused on the non-numeric
``excluded_stations`` field (stations the planner skips when sizing buildable
units, persisted per facility)."""
import core.facility_settings_storage as fss
from core.facility_settings_storage import _clean_excluded, get_facility_settings


class TestCleanExcluded:
    def test_list_is_cleaned_and_deduped(self):
        assert _clean_excluded(
            ["Battery Assembly", " QC ", "Battery Assembly", "", None]
        ) == ["Battery Assembly", "QC"]

    def test_non_list_returns_empty(self):
        assert _clean_excluded(None) == []
        assert _clean_excluded("Battery Assembly") == []   # a bare string is not a list
        assert _clean_excluded(42) == []

    def test_preserves_first_seen_order(self):
        assert _clean_excluded(["Ship", "QC", "Ship", "PDI"]) == ["Ship", "QC", "PDI"]


class TestGetFacilitySettings:
    def test_always_includes_excluded_stations_list(self):
        # Whatever is on disk, the merged result carries excluded_stations as a list.
        out = get_facility_settings("Henderson")
        assert isinstance(out["excluded_stations"], list)

    def test_reads_saved_excluded(self, monkeypatch):
        monkeypatch.setattr(
            fss, "load_all_facility_settings",
            lambda: {"Henderson": {"safety": 0.9,
                                   "excluded_stations": ["Battery Assembly", "Battery Assembly"]}},
        )
        out = get_facility_settings("Henderson")
        assert out["excluded_stations"] == ["Battery Assembly"]   # deduped
        assert out["safety"] == 0.9

    def test_missing_key_defaults_empty(self, monkeypatch):
        monkeypatch.setattr(
            fss, "load_all_facility_settings",
            lambda: {"Henderson": {"safety": 0.9}},
        )
        assert get_facility_settings("Henderson")["excluded_stations"] == []
