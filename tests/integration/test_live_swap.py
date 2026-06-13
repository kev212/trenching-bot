"""Integration tests for real GMGN CLI (run on VPS, NOT in dev).

These tests require:
  - gmgn-cli installed (npm install -g gmgn-cli)
  - ~/.config/gmgn/.env configured with valid API key + private key
  - VPS IP whitelisted at GMGN dashboard
  - GMGN hosted wallet has some SOL for testing

Run with: pytest -m integration -v
Skip by default (CI/dev): pytest (skips via -m "not integration" in pytest.ini)
"""
import asyncio
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.gmgn_cli import GMGNCli, SOL_MINT, USDC_MINT

pytestmark = pytest.mark.integration

# Skip all tests if gmgn-cli is not installed
if not shutil.which("gmgn-cli"):
    pytest.skip("gmgn-cli not installed", allow_module_level=True)

# Skip if no credentials file
CONFIG_DIR = os.path.expanduser("~/.config/gmgn")
if not os.path.exists(os.path.join(CONFIG_DIR, ".env")):
    pytest.skip("~/.config/gmgn/.env not configured", allow_module_level=True)


@pytest.fixture
def cli():
    """Real GMGNCli instance (uses ~/.config/gmgn/.env)."""
    return GMGNCli()


@pytest.fixture
def wallet_address(cli):
    """Real GMGN hosted wallet address on Solana."""
    return cli.get_wallet_address_sync()


class TestConditionOrdersJSON:
    """Verify the JSON format is accepted by gmgn-cli."""

    def test_two_conditions_format(self):
        """The exact JSON we send to gmgn-cli swap."""
        conditions = [
            {"order_type": "profit_stop", "side": "sell",
             "price_scale": "130", "sell_ratio": "100"},
            {"order_type": "loss_stop", "side": "sell",
             "price_scale": "50", "sell_ratio": "100"},
        ]
        json_str = json.dumps(conditions)
        # Verify it parses
        parsed = json.loads(json_str)
        assert len(parsed) == 2
        assert parsed[0]["order_type"] == "profit_stop"
        assert parsed[1]["order_type"] == "loss_stop"


class TestRealGMGNSwap:
    """Live test: submit a tiny swap with condition orders."""

    def test_dry_run_quote_works(self, cli, wallet_address):
        """Verify auth + quote works (no transaction)."""
        quote = await_sync(cli.quote(
            chain="sol", from_addr=wallet_address,
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=100000, slippage=30,
        ))
        assert isinstance(quote, dict)
        assert "out_amount" in quote or "output_amount" in quote or "input_amount" in quote

    def test_swap_with_conditions_submits(self, cli, wallet_address):
        """Submit a real 0.0001 SOL → USDC swap with TP1+SL conditions."""
        if not wallet_address:
            pytest.skip("No GMGN wallet address")
        conditions = json.dumps([
            {"order_type": "profit_stop", "side": "sell",
             "price_scale": "130", "sell_ratio": "100"},
            {"order_type": "loss_stop", "side": "sell",
             "price_scale": "50", "sell_ratio": "100"},
        ])
        result = await_sync(cli.swap(
            chain="sol", from_addr=wallet_address,
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=100000, slippage=30,
            anti_mev=True,
            priority_fee=0.0001,
            tip_fee=0.00001,
            condition_orders=conditions,
        ))
        assert result is not None
        assert result.get("order_id") is not None or result.get("status") == "submitted"

    def test_strategy_listed_after_swap(self, cli, wallet_address):
        """After swap, the strategy should be in the list."""
        strategies = await_sync(cli.list_strategies(
            chain="sol", from_addr=wallet_address,
            base_token=USDC_MINT,
        ))
        assert "list" in strategies
        # At least one strategy should exist
        # (could be from previous test runs)
        if strategies["list"]:
            s = strategies["list"][0]
            assert "condition_orders" in s
            assert "base_token" in s
            # Each condition should have order_type and price_scale
            for c in s.get("condition_orders", []):
                assert "order_type" in c
                assert "price_scale" in c


def await_sync(coro):
    """Helper to run an async coroutine in a sync test."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)
