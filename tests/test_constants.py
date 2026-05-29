"""Tests for pure business-rule helpers in core.constants."""
from datetime import date

from core.constants import get_battery_type, week_of_month


class TestGetBatteryType:
    def test_boss25_family(self):
        assert get_battery_type("BOSS25-006") == "25-12"
        assert get_battery_type("BOSS25-010") == "25-12"

    def test_boss70_hybrid(self):
        # BOSS70-40 Hybrid SKUs use the 70-16 pack.
        assert get_battery_type("BOSS70-012") == "70-16"
        assert get_battery_type("BOSS70-001") == "70-16"

    def test_boss70_other(self):
        assert get_battery_type("BOSS70-017") == "70-20"
        assert get_battery_type("BOSS70-019") == "70-20"

    def test_boss125_220_400(self):
        assert get_battery_type("BOSS125-004") == "125-20"
        assert get_battery_type("BOSS220HS-002") == "125-20"
        assert get_battery_type("BOSS400HS-002") == "125-20"

    def test_unknown(self):
        assert get_battery_type("PDS-100") == "UNKNOWN"
        assert get_battery_type("") == "UNKNOWN"

    def test_case_and_whitespace_normalized(self):
        # Regression (audit fix): the hybrid check previously compared the raw
        # fg_base, so lower-case / whitespace-padded inputs mis-classified as
        # 70-20. They must normalize to 70-16.
        assert get_battery_type("boss70-012") == "70-16"
        assert get_battery_type("  BOSS70-012  ") == "70-16"
        assert get_battery_type("boss70-001") == "70-16"


class TestWeekOfMonth:
    def test_june_2026_first_is_monday(self):
        # June 1 2026 is a Monday → clean Mon-anchored weeks.
        assert week_of_month(date(2026, 6, 1)) == 1
        assert week_of_month(date(2026, 6, 8)) == 2
        assert week_of_month(date(2026, 6, 30)) == 5

    def test_may_2026_first_is_friday(self):
        # May 1 2026 is a Friday → partial first week.
        assert week_of_month(date(2026, 5, 1)) == 1
        assert week_of_month(date(2026, 5, 4)) == 2
        assert week_of_month(date(2026, 5, 8)) == 2
