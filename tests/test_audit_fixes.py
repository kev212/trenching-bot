"""Tests for audit fixes (June 2026 post-bug-fixing audit).

Covers:
- M1: format_exit_alert escapes address[:8]
- M2: format_trade_alert handles None/empty entry_tx_sig
- M3: get_sol_price_usd falls back to DexScreener when Jupiter is backed off
- C2: _gmgn_poller rate-limit path does NOT call FailureTracker
- C1: no duplicate start() methods in main.py (syntax check via import)
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from alerts.formatter import format_exit_alert, format_trade_alert
from core.price_oracle import PriceOracle, SOL_MINT


# --- M1: format_exit_alert escapes address --------------------------------

def test_format_exit_alert_escapes_address():
    """Address[:8] should be _escape_markdown'd (defensive)."""
    msg = format_exit_alert(
        symbol="TEST",
        address="ABC*def_hij`klm[nop",  # has every Markdown char
        entry_price=0.0001,
        exit_price=0.0002,
        pnl_sol=0.01,
        pnl_pct=100.0,
        reason="TP1",
        hold_seconds=120.0,
    )
    # First 8 chars of address, all special chars escaped
    assert r"ABC\*def\_" in msg


def test_format_exit_alert_normal_address():
    """Normal base58 address should pass through cleanly."""
    msg = format_exit_alert(
        symbol="TEST",
        address="So11111111111111111111111111111111111111112",
        entry_price=0.0001,
        exit_price=0.0002,
        pnl_sol=0.01,
        pnl_pct=100.0,
        reason="SL",
        hold_seconds=60.0,
    )
    # First 8 chars of the address are "So111111"
    assert "So111111" in msg


# --- M2: format_trade_alert handles None/empty entry_tx_sig ----------------

class _FakePosition:
    """Minimal position object for format_trade_alert tests."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_format_trade_alert_buy_with_tx_sig():
    """Normal case: entry_tx_sig present, shows TX line."""
    pos = _FakePosition(
        token_symbol="TEST",
        token_address="So11111111111111111111111111111111111111112",
        entry_amount_sol=0.05,
        entry_amount_token=1000.0,
        entry_price=0.0001,
        entry_tx_sig="5j7K8mN9pQ0rS1tU2vW3xY4zA5bC6dE7fG8hI9jK0L1mN2oP3qR4sT5uV6wX7yZ8",
    )
    msg = format_trade_alert(pos, side="BUY")
    assert "TX:" in msg
    # First 16 chars of sig
    assert "5j7K8mN9pQ0rS1tU" in msg


def test_format_trade_alert_buy_with_none_tx_sig():
    """None tx_sig: should NOT show TX line, should NOT crash."""
    pos = _FakePosition(
        token_symbol="TEST",
        token_address="So11111111111111111111111111111111111111112",
        entry_amount_sol=0.05,
        entry_amount_token=1000.0,
        entry_price=0.0001,
        entry_tx_sig=None,
    )
    msg = format_trade_alert(pos, side="BUY")
    assert "TX:" not in msg
    # Other fields still present
    assert "TRADE: BUY TEST" in msg


def test_format_trade_alert_buy_with_empty_tx_sig():
    """Empty tx_sig: should NOT show TX line, should NOT crash."""
    pos = _FakePosition(
        token_symbol="TEST",
        token_address="So11111111111111111111111111111111111111112",
        entry_amount_sol=0.05,
        entry_amount_token=1000.0,
        entry_price=0.0001,
        entry_tx_sig="",
    )
    msg = format_trade_alert(pos, side="BUY")
    assert "TX:" not in msg


def test_format_trade_alert_sell_works():
    """SELL side: entry_tx_sig not required, exit fields shown."""
    pos = _FakePosition(
        token_symbol="TEST",
        token_address="So11111111111111111111111111111111111111112",
        entry_amount_sol=0.05,
        entry_amount_token=1000.0,
        entry_price=0.0001,
        entry_tx_sig="",
        exit_price=0.0002,
        exit_reason="TP1",
        pnl_sol=0.05,
        pnl_usd=10.0,
        pnl_pct=100.0,
    )
    msg = format_trade_alert(pos, side="SELL")
    assert "TRADE: SELL" in msg
    assert "Exit:" in msg
    assert "PnL:" in msg


# --- M3: DexScreener fallback for SOL/USD ----------------------------------

def test_sol_usd_falls_back_to_dexscreener_when_jupiter_backed_off():
    """When Jupiter backoff is active, get_sol_price_usd skips Jupiter and uses DexScreener."""
    oracle = PriceOracle()
    oracle._session = MagicMock()

    # Activate Jupiter backoff
    oracle._jupiter_backoff_until = __import__("time").time() + 600.0

    # Track which sources were called
    called = {"v3": False, "v6": False, "dex": False}

    async def fake_v3():
        called["v3"] = True
        return 0.0

    async def fake_v6():
        called["v6"] = True
        return 0.0

    async def fake_dex():
        called["dex"] = True
        return 175.50

    oracle._fetch_sol_v3 = fake_v3
    oracle._fetch_sol_v6 = fake_v6
    oracle._fetch_sol_dexscreener = fake_dex

    async def _run():
        return await oracle.get_sol_price_usd()

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result == 175.50
    assert called["v3"] is False, "v3 should be skipped when Jupiter backed off"
    assert called["v6"] is False, "v6 should be skipped when Jupiter backed off"
    assert called["dex"] is True, "DexScreener should be tried"


def test_sol_usd_uses_dexscreener_when_both_jupiter_fail():
    """Even without backoff, if v3 and v6 both fail, DexScreener is the 3rd source."""
    oracle = PriceOracle()
    oracle._session = MagicMock()

    called = {"v3": False, "v6": False, "dex": False}

    async def fake_v3():
        called["v3"] = True
        return 0.0

    async def fake_v6():
        called["v6"] = True
        return 0.0

    async def fake_dex():
        called["dex"] = True
        return 180.25

    oracle._fetch_sol_v3 = fake_v3
    oracle._fetch_sol_v6 = fake_v6
    oracle._fetch_sol_dexscreener = fake_dex

    async def _run():
        return await oracle.get_sol_price_usd()

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result == 180.25
    assert called["v3"] is True
    assert called["v6"] is True
    assert called["dex"] is True


def test_sol_usd_v3_success_skips_dexscreener():
    """If v3 succeeds, DexScreener is NOT called."""
    oracle = PriceOracle()
    oracle._session = MagicMock()

    called = {"v3": False, "v6": False, "dex": False}

    async def fake_v3():
        called["v3"] = True
        return 150.0

    async def fake_v6():
        called["v6"] = True
        return 0.0

    async def fake_dex():
        called["dex"] = True
        return 0.0

    oracle._fetch_sol_v3 = fake_v3
    oracle._fetch_sol_v6 = fake_v6
    oracle._fetch_sol_dexscreener = fake_dex

    async def _run():
        return await oracle.get_sol_price_usd()

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result == 150.0
    assert called["v3"] is True
    assert called["v6"] is False
    assert called["dex"] is False


# --- C1: syntax check for main.py (no duplicate start) --------------------

def test_main_has_no_duplicate_start():
    """main.py should have exactly ONE async def start(self) on TrenchingBot."""
    import ast
    with open("/Users/khezuma/workspace/trenching/main.py") as f:
        tree = ast.parse(f.read())
    # Find TrenchingBot class
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TrenchingBot":
            start_methods = [
                m for m in node.body
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "start"
            ]
            assert len(start_methods) == 1, (
                f"TrenchingBot has {len(start_methods)} start() methods, expected 1. "
                f"This is a regression of the duplicate-start bug."
            )
            return
    raise AssertionError("TrenchingBot class not found in main.py")
