"""Tests for KOL presence hard gate filter."""
import sys
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from analysis.models import TokenData, FeatureVector
from analysis.filters import _filter_kol_presence, run_all_filters, count_passed_filters


def test_kol_filter_fails_zero():
    """0 KOL wallets → fails."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 0
    t.kol_still_holding = 0
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is False
    assert result["renowned_wallets"] == 0
    assert result["kol_still_holding"] == 0


def test_kol_filter_passes_one():
    """1 KOL wallet holding → passes (min=1)."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 1
    t.kol_still_holding = 1
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is True
    assert result["renowned_wallets"] == 1
    assert result["kol_still_holding"] == 1


def test_kol_filter_passes_multiple():
    """3 KOL wallets holding → passes."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 3
    t.kol_still_holding = 3
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is True
    assert result["renowned_wallets"] == 3
    assert result["kol_still_holding"] == 3


def test_kol_filter_disabled():
    """Disabled → result passed=False but count_passed skips it."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 0
    t.kol_still_holding = 0
    result = _filter_kol_presence(t, {"enabled": False, "min_holding": 1})
    assert result["enabled"] is False
    assert result["passed"] is False


def test_kol_filter_run_all_filters():
    """Verify kol_presence is in fv when run_all_filters is called."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 2
    t.kol_still_holding = 2
    fv = run_all_filters(t, {"kol_presence": {"enabled": True, "min_holding": 1}})
    assert fv.kol_presence.get("passed") is True
    assert fv.kol_presence.get("renowned_wallets") == 2
    fv_dict = fv.to_dict()
    assert "kol_presence" in fv_dict


def test_kol_filter_count_passed():
    """count_passed_filters includes kol_presence."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 0
    t.kol_still_holding = 0
    fv = run_all_filters(t, {"kol_presence": {"enabled": True, "min_holding": 1}})
    passed, total, failures = count_passed_filters(fv)
    assert "kol_presence" in failures


def test_kol_filter_count_passed_disabled():
    """Disabled kol_presence is skipped in count."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 0
    t.kol_still_holding = 0
    fv = run_all_filters(t, {"kol_presence": {"enabled": False, "min_holding": 1}})
    passed, total, failures = count_passed_filters(fv)
    assert "kol_presence" not in failures


def test_kol_still_holding_fails_zero():
    """kol_still_holding=0 → fails even if renowned_wallets>0."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 3
    t.kol_still_holding = 0
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is False
    assert result["renowned_wallets"] == 3
    assert result["kol_still_holding"] == 0


def test_kol_still_holding_passes_one():
    """kol_still_holding=1 → passes."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 5
    t.kol_still_holding = 1
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is True
    assert result["kol_still_holding"] == 1


def test_kol_holding_over_renowned():
    """renowned=2, kol_still_holding=0 → KOLs dumped, fails."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 2
    t.kol_still_holding = 0
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is False
    assert result["kol_still_holding"] == 0


def test_kol_holding_passes_over_min():
    """kol_still_holding=2, min=1 → passes."""
    t = TokenData(address="test", symbol="TST")
    t.renowned_wallets = 2
    t.kol_still_holding = 2
    result = _filter_kol_presence(t, {"enabled": True, "min_holding": 1})
    assert result["passed"] is True
    assert result["kol_still_holding"] == 2
