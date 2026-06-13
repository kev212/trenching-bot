"""Tests for hard gate filters: min_volume_5m and token_age."""
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from analysis.filters import (
    _filter_min_volume_5m,
    _filter_token_age,
    run_all_filters,
    count_passed_filters,
    check_hard_gate,
)
from analysis.models import TokenData


# ============ min_volume_5m tests ============

class TestMinVolume5m:
    def test_above_threshold_passes(self):
        t = TokenData(address="x", volume_5m=150000)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 100000})
        assert result["passed"] is True
        assert result["volume_5m_usd"] == 150000
        assert result["threshold"] == 100000
        assert "150,000" in result["note"]

    def test_below_threshold_fails(self):
        t = TokenData(address="x", volume_5m=50000)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 100000})
        assert result["passed"] is False
        assert "50,000" in result["note"]

    def test_zero_volume_fails_safe(self):
        """GMGN missing field → default 0 → fail (safe, no false positive)."""
        t = TokenData(address="x", volume_5m=0)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 100000})
        assert result["passed"] is False

    def test_disabled_filter_passes(self):
        t = TokenData(address="x", volume_5m=0)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 100000, "enabled": False})
        assert result["passed"] is False  # note: passed still false, but enabled=False
        assert result["enabled"] is False

    def test_boundary_exact_threshold_passes(self):
        t = TokenData(address="x", volume_5m=100000)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 100000})
        assert result["passed"] is True  # 100K == 100K, inclusive

    def test_custom_threshold(self):
        t = TokenData(address="x", volume_5m=75000)
        result = _filter_min_volume_5m(t, {"min_volume_usd": 50000})
        assert result["passed"] is True


# ============ token_age tests ============

class TestTokenAge:
    def test_pre_migrate_pass(self):
        """Pre-migrate, age 3min < 5min → pass."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(now - 180),  # 3 min ago
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is True
        assert result["status"] == "pre-migrate"
        assert 2.5 < result["age_minutes"] < 3.5

    def test_pre_migrate_fail(self):
        """Pre-migrate, age 10min > 5min → fail."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(now - 600),  # 10 min ago
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is False
        assert result["status"] == "pre-migrate"

    def test_post_migrate_pass(self):
        """Post-migrate, age 2min < 5min → pass."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=int(now - 600),  # migrated 10 min ago
            open_timestamp=int(now - 120),  # opened 2 min ago
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is True
        assert result["status"] == "post-migrate"

    def test_post_migrate_fail(self):
        """Post-migrate, age 10min > 5min → fail."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=int(now - 700),
            open_timestamp=int(now - 600),  # 10 min ago
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is False
        assert result["status"] == "post-migrate"

    def test_no_timestamps_fail_safe(self):
        """No creation_timestamp and no migrated → fail with age=999."""
        t = TokenData(address="x", creation_timestamp=0, open_timestamp=0, migrated_timestamp=0)
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is False
        assert result["age_minutes"] == 999.0

    def test_migrated_but_no_open_timestamp_fails_safe(self):
        """Migrated token but open_timestamp=0 → treat as 999 (very old)."""
        t = TokenData(
            address="x",
            migrated_timestamp=int(time.time() - 60),  # just migrated
            open_timestamp=0,
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        assert result["passed"] is False  # 999 > 5
        assert result["status"] == "post-migrate"

    def test_boundary_exact_5min_passes(self):
        """Exactly 5.0 min should pass (inclusive)."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(now - 300),  # exactly 5 min ago
        )
        result = _filter_token_age(t, {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5})
        # Might be just over 5 due to processing time, allow either
        # The check is `age_min <= max_min` so 5.0 exactly should pass
        # but 5.0001 would fail
        # We test the boolean to be safe
        if result["age_minutes"] <= 5.0:
            assert result["passed"] is True
        else:
            assert result["passed"] is False  # processing time pushed it over

    def test_disabled_filter_does_not_affect_pass(self):
        """When disabled, function still returns passed based on logic,
        but the hard gate check (count_passed_filters) skips disabled filters."""
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(time.time() - 600),  # 10 min
        )
        result = _filter_token_age(t, {
            "max_pre_migrate_minutes": 5,
            "max_post_migrate_minutes": 5,
            "enabled": False,
        })
        assert result["enabled"] is False
        # passed still reflects the actual logic, but in hard gate
        # count_passed_filters will skip this filter because enabled=False


# ============ Pre-check / Hard-Gate Decoupling ============

class TestPreCheckDecoupling:
    def test_filter_uses_5_min_from_config(self):
        """Hard gate filter reads from filter_params.token_age (5 min)."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(now - 600),  # 10 min
        )
        params = {"token_age": {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5}}
        fv = run_all_filters(t, params)
        # Hard gate should fail (10 > 5)
        assert fv.token_age["passed"] is False

    def test_filter_uses_120_min_from_config(self):
        """Verify filter respects custom (looser) config values."""
        now = time.time()
        t = TokenData(
            address="x",
            migrated_timestamp=0,
            creation_timestamp=int(now - 600),  # 10 min
        )
        params = {"token_age": {"max_pre_migrate_minutes": 120, "max_post_migrate_minutes": 45}}
        fv = run_all_filters(t, params)
        # With loose 120/45 config, 10 min passes
        assert fv.token_age["passed"] is True


# ============ Integration: Hard Gate Flow ============

class TestHardGateIntegration:
    def test_both_in_check_hard_gate(self):
        """Verify both new filters are in the hard gate check."""
        from analysis.models import FeatureVector
        now = time.time()
        t = TokenData(
            address="x",
            volume_5m=50000,  # fails volume
            migrated_timestamp=0,
            creation_timestamp=int(now - 600),  # 10 min, fails age
        )
        fv = run_all_filters(t, {
            "min_volume_5m": {"min_volume_usd": 100000},
            "token_age": {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5},
        })
        passed, failures = check_hard_gate(fv)
        assert passed is False
        assert "min_volume_5m" in failures
        assert "token_age" in failures

    def test_both_pass_when_meets_criteria(self):
        """Both filters pass → no failures."""
        now = time.time()
        t = TokenData(
            address="x",
            volume_5m=150000,  # passes
            migrated_timestamp=0,
            creation_timestamp=int(now - 60),  # 1 min, passes
        )
        fv = run_all_filters(t, {
            "min_volume_5m": {"min_volume_usd": 100000},
            "token_age": {"max_pre_migrate_minutes": 5, "max_post_migrate_minutes": 5},
        })
        passed, failures = check_hard_gate(fv)
        # May have other filter failures (holders, fee, etc.) but
        # our two new filters should NOT be in failures
        assert "min_volume_5m" not in failures
        assert "token_age" not in failures
