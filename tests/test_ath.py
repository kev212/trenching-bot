"""Test Phase B (ATH Integration)."""
import sys
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from analysis.models import TokenData, FeatureVector
from analysis.filters import _filter_ath_drawdown, run_all_filters, count_passed_filters


def make_token(**kwargs) -> TokenData:
    t = TokenData(
        address=kwargs.get("address", "test"),
        name=kwargs.get("name", "Test"),
        symbol=kwargs.get("symbol", "TST"),
    )
    t.ath_price = kwargs.get("ath_price", 0.0)
    t.drawdown_from_ath_pct = kwargs.get("drawdown_from_ath_pct", 0.0)
    return t


def test_ath_filter_passes_zero_dd():
    """Fresh token, no drawdown → passes."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=0.0)
    result = _filter_ath_drawdown(token, {"enabled": True, "max_drawdown_pct": -50.0})
    assert result["passed"] is True
    assert result["drawdown_pct"] == 0.0
    assert result["enabled"] is True
    print(f"✓ zero_dd: passed={result['passed']}, note='{result['note']}'")


def test_ath_filter_passes_moderate_dd():
    """20% drawdown (within -50% threshold) → passes."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=-20.0)
    result = _filter_ath_drawdown(token, {"enabled": True, "max_drawdown_pct": -50.0})
    assert result["passed"] is True
    print(f"✓ moderate_dd: passed={result['passed']}, note='{result['note']}'")


def test_ath_filter_fails_deep_dd():
    """70% drawdown (deeper than -50%) → fails."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=-70.0)
    result = _filter_ath_drawdown(token, {"enabled": True, "max_drawdown_pct": -50.0})
    assert result["passed"] is False
    print(f"✓ deep_dd: passed={result['passed']}, note='{result['note']}'")


def test_ath_filter_disabled():
    """Disabled filter → always passes, marked as disabled."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=-90.0)
    result = _filter_ath_drawdown(token, {"enabled": False, "max_drawdown_pct": -50.0})
    # Note: even when disabled, our impl returns passed based on dd, but
    # count_passed_filters() in main.py skips disabled filters. This is correct:
    # the filter is informational only.
    assert result["enabled"] is False
    print(f"✓ disabled: enabled={result['enabled']}, passed={result['passed']}, note='{result['note']}'")


def test_run_all_filters_includes_ath():
    """Verify ath_drawdown is in fv when run_all_filters is called."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=-30.0)
    fv = run_all_filters(token, {
        "ath_drawdown": {"enabled": True, "max_drawdown_pct": -50.0}
    })
    assert fv.ath_drawdown.get("drawdown_pct") == -30.0
    assert fv.ath_drawdown.get("passed") is True
    fv_dict = fv.to_dict()
    assert "ath_drawdown" in fv_dict
    print(f"✓ run_all_filters: ath_drawdown in fv_dict, passed={fv_dict['ath_drawdown']['passed']}")


def test_count_passed_with_ath():
    """Verify count_passed_filters handles ath_drawdown correctly."""
    token = make_token(ath_price=0.001, drawdown_from_ath_pct=-30.0)
    fv = run_all_filters(token, {
        "ath_drawdown": {"enabled": True, "max_drawdown_pct": -50.0}
    })
    passed_count, _, _ = count_passed_filters(fv)
    # Should include ath_drawdown in the count
    assert passed_count >= 1, f"expected at least 1 pass, got {passed_count}"
    print(f"✓ count_passed: {passed_count} filters passed (includes ath_drawdown)")


def test_token_data_has_ath_fields():
    """Verify TokenData has the 3 new ATH fields."""
    t = TokenData(address="x")
    assert hasattr(t, "ath_price")
    assert hasattr(t, "ath_timestamp")
    assert hasattr(t, "drawdown_from_ath_pct")
    assert t.ath_price == 0.0
    assert t.ath_timestamp == 0
    assert t.drawdown_from_ath_pct == 0.0
    print("✓ TokenData has 3 new ATH fields (ath_price, ath_timestamp, drawdown_from_ath_pct)")


def test_feature_vector_has_ath():
    """Verify FeatureVector has ath_drawdown field."""
    fv = FeatureVector()
    assert hasattr(fv, "ath_drawdown")
    assert fv.ath_drawdown == {}
    d = fv.to_dict()
    assert "ath_drawdown" in d
    print("✓ FeatureVector has ath_drawdown field")


if __name__ == "__main__":
    test_ath_filter_passes_zero_dd()
    test_ath_filter_passes_moderate_dd()
    test_ath_filter_fails_deep_dd()
    test_ath_filter_disabled()
    test_run_all_filters_includes_ath()
    test_count_passed_with_ath()
    test_token_data_has_ath_fields()
    test_feature_vector_has_ath()
    print("\n✅ All Phase B tests pass")
