"""Tests for GMGN condition orders (TP1/TP2/SL) + strategy_poller."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from analysis.models import TokenData


# ============ _build_condition_orders_json ============

class TestBuildConditionOrders:
    def _executor(self, config):
        """Build a TradeExecutor-like object that exposes _build_condition_orders_json."""
        from core.trade_executor import TradeExecutor
        ex = TradeExecutor.__new__(TradeExecutor)
        ex.config = config
        return ex

    def test_default_config(self):
        config = {
            "tp1_multiplier": 1.30,
            "tp1_sell_pct": 80,
            "tp2_multiplier": 2.00,
            "tp2_sell_pct": 100,
            "stop_loss_pct": 50,
        }
        ex = self._executor(config)
        result = json.loads(ex._build_condition_orders_json())
        assert len(result) == 3

        tp1, tp2, sl = result
        assert tp1["order_type"] == "profit_stop"
        assert tp1["price_scale"] == "130"
        assert tp1["sell_ratio"] == "80"
        assert tp1["sell_ratio_type"] == "hold_amount"

        assert tp2["price_scale"] == "200"
        assert tp2["sell_ratio"] == "100"

        assert sl["order_type"] == "loss_stop"
        assert sl["price_scale"] == "50"
        assert sl["sell_ratio"] == "100"

    def test_custom_config(self):
        config = {
            "tp1_multiplier": 1.50,
            "tp1_sell_pct": 50,
            "tp2_multiplier": 3.00,
            "tp2_sell_pct": 100,
            "stop_loss_pct": 30,
        }
        ex = self._executor(config)
        result = json.loads(ex._build_condition_orders_json())
        tp1, tp2, sl = result
        assert tp1["price_scale"] == "150"
        assert tp1["sell_ratio"] == "50"
        assert tp2["price_scale"] == "300"
        assert sl["price_scale"] == "70"  # 100 - 30

    def test_missing_config_uses_defaults(self):
        ex = self._executor({})
        result = json.loads(ex._build_condition_orders_json())
        tp1, tp2, sl = result
        assert tp1["price_scale"] == "130"
        assert tp2["price_scale"] == "200"
        assert sl["price_scale"] == "50"

    def test_returns_valid_json(self):
        ex = self._executor({})
        result = ex._build_condition_orders_json()
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert all(isinstance(o, dict) for o in parsed)


# ============ _execute_buy_live passes condition_orders to swap ============

class TestBuyLiveUsesConditionOrders:
    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_buy_passes_condition_orders(self, mock_exec):
        from core.trade_executor import TradeExecutor, SOL_MINT
        from core.wallet import Wallet, PAPER_PUBKEY
        from core.position_manager import PositionManager

        # Mock process
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(
            return_value=(json.dumps({
                "order_id": "ord_test",
                "strategy_order_id": "strat_abc",
                "status": "submitted",
            }).encode(), b"")
        )
        mock_exec.return_value = mock_process

        # Mock the wait_for_order response
        wait_response = json.dumps({
            "status": "confirmed",
            "hash": "tx_test",
            "report": {
                "output_amount": "1000000",
                "output_token_decimals": 6,
            }
        }).encode()

        wallet = Wallet(paper=True, starting_balance_sol=10.0)
        wallet.RESERVE_SOL = 0.1

        # Mock gmgn_cli
        gmgn_cli = MagicMock()
        gmgn_cli.get_sol_balance = AsyncMock(return_value=10.0)
        gmgn_cli.swap = AsyncMock(return_value={
            "order_id": "ord_test",
            "strategy_order_id": "strat_abc",
        })
        gmgn_cli.wait_for_order = AsyncMock(return_value={
            "status": "confirmed",
            "hash": "tx_test",
            "report": {
                "output_amount": "1000000",
                "output_token_decimals": 6,
            }
        })

        # Mock position manager
        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock(return_value=42)
        pos_mgr.record_trade = AsyncMock(return_value=1)
        pos_mgr.db = MagicMock()
        pos_mgr.db.save_risk_event = AsyncMock()

        # Mock risk manager
        risk = MagicMock()

        # Build executor
        ex = TradeExecutor(
            paper=False,
            wallet=wallet,
            jupiter=MagicMock(),
            positions=pos_mgr,
            risk=risk,
            config={
                "tp1_multiplier": 1.30,
                "tp1_sell_pct": 80,
                "tp2_multiplier": 2.00,
                "tp2_sell_pct": 100,
                "stop_loss_pct": 50,
                "slippage_bps": 300,
            },
            gmgn=None,
            price_oracle=None,
            gmgn_cli=gmgn_cli,
        )

        # Build a fake token
        token = TokenData(
            address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            symbol="USDC",
            name="USD Coin",
        )

        result = await ex._execute_buy_live(token, size_sol=0.05, filter_params_version=1)

        # Verify gmgn_cli.swap was called with condition_orders
        assert gmgn_cli.swap.called
        call_kwargs = gmgn_cli.swap.call_args.kwargs
        assert "condition_orders" in call_kwargs
        conditions = json.loads(call_kwargs["condition_orders"])
        assert len(conditions) == 3
        assert conditions[0]["order_type"] == "profit_stop"
        assert conditions[0]["price_scale"] == "130"
        assert conditions[2]["order_type"] == "loss_stop"

    async def test_buy_skipped_on_insufficient_balance(self):
        from core.trade_executor import TradeExecutor
        from core.wallet import Wallet
        from core.position_manager import PositionManager

        wallet = Wallet(paper=True, starting_balance_sol=0.0)
        wallet.RESERVE_SOL = 0.1

        gmgn_cli = MagicMock()
        gmgn_cli.get_sol_balance = AsyncMock(return_value=0.05)  # below reserve

        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock()
        risk = MagicMock()

        ex = TradeExecutor(
            paper=False, wallet=wallet, jupiter=MagicMock(),
            positions=pos_mgr, risk=risk,
            config={"slippage_bps": 300},
            gmgn_cli=gmgn_cli,
        )

        token = TokenData(
            address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            symbol="USDC", name="USD Coin",
        )
        result = await ex._execute_buy_live(token, size_sol=0.05)
        assert result is None
        gmgn_cli.swap.assert_not_called()


# ============ strategy_poller ============

class TestStrategyPoller:
    async def test_polls_live_positions_only(self):
        from tracking.strategy_poller import _tick

        gmgn_cli = MagicMock()
        gmgn_cli.get_wallet_address = AsyncMock(return_value="WALLET")
        gmgn_cli.list_strategies = AsyncMock(return_value={"list": []})

        pos_mgr = MagicMock()
        # Mix of paper and live; only live should be checked
        pos_mgr.get_open_positions = AsyncMock(return_value=[
            {"id": 1, "paper": 1, "token_address": "TokP1", "token_symbol": "P1",
             "tp1_filled": 0, "tp2_filled": 0, "sl_filled": 0},
            {"id": 2, "paper": 0, "token_address": "TokL1", "token_symbol": "L1",
             "tp1_filled": 0, "tp2_filled": 0, "sl_filled": 0},
        ])
        pos_mgr.update_position = AsyncMock()

        await _tick(MagicMock(), pos_mgr, gmgn_cli)
        # Only id=2 (live) should be queried
        assert gmgn_cli.list_strategies.call_count == 1
        gmgn_cli.list_strategies.assert_called_with(
            chain="sol", from_addr="WALLET", base_token="TokL1",
        )

    async def test_tp1_filled_updates_position(self):
        from tracking.strategy_poller import _check_one

        gmgn_cli = MagicMock()
        gmgn_cli.list_strategies = AsyncMock(return_value={
            "list": [{
                "base_token": "TokPEPE111",
                "status": "open",
                "strategy_status": "running",
                "condition_orders": [
                    {"order_type": "profit_stop", "price_scale": "130",
                     "sell_ratio": "80", "status": "filled"},
                    {"order_type": "profit_stop", "price_scale": "200",
                     "sell_ratio": "100", "status": "check"},
                    {"order_type": "loss_stop", "price_scale": "50",
                     "sell_ratio": "100", "status": "check"},
                ],
            }],
        })

        pos = {
            "id": 5, "token_symbol": "PEPE", "token_address": "TokPEPE111",
            "paper": 0, "tp1_filled": 0, "tp2_filled": 0, "sl_filled": 0,
        }
        pos_mgr = MagicMock()
        pos_mgr.update_position = AsyncMock()
        pos_mgr.close_position = AsyncMock()

        await _check_one(pos, pos_mgr, gmgn_cli, "WALLET")

        assert pos["tp1_filled"] == 1
        pos_mgr.update_position.assert_called_once()
        # Not closed yet — TP2 still pending
        pos_mgr.close_position.assert_not_called()

    async def test_tp1_and_tp2_filled_closes_position(self):
        from tracking.strategy_poller import _check_one

        gmgn_cli = MagicMock()
        gmgn_cli.list_strategies = AsyncMock(return_value={
            "list": [{
                "base_token": "TokWIF111",
                "status": "open",
                "strategy_status": "running",
                "condition_orders": [
                    {"order_type": "profit_stop", "price_scale": "130",
                     "sell_ratio": "80", "status": "filled"},
                    {"order_type": "profit_stop", "price_scale": "200",
                     "sell_ratio": "100", "status": "filled"},
                    {"order_type": "loss_stop", "price_scale": "50",
                     "sell_ratio": "100", "status": "check"},
                ],
            }],
        })

        pos = {
            "id": 6, "token_symbol": "WIF", "token_address": "TokWIF111",
            "paper": 0, "tp1_filled": 1, "tp2_filled": 0, "sl_filled": 0,
        }
        pos_mgr = MagicMock()
        pos_mgr.update_position = AsyncMock()
        pos_mgr.close_position = AsyncMock()

        await _check_one(pos, pos_mgr, gmgn_cli, "WALLET")

        assert pos["tp2_filled"] == 1
        pos_mgr.update_position.assert_called_once()
        pos_mgr.close_position.assert_called_once()
        args, kwargs = pos_mgr.close_position.call_args
        assert "TP1+TP2" in (kwargs.get("exit_reason") or args[1])

    async def test_sl_filled_closes_position(self):
        from tracking.strategy_poller import _check_one

        gmgn_cli = MagicMock()
        gmgn_cli.list_strategies = AsyncMock(return_value={
            "list": [{
                "base_token": "TokDOGE111",
                "status": "open",
                "strategy_status": "running",
                "condition_orders": [
                    {"order_type": "profit_stop", "price_scale": "130",
                     "sell_ratio": "80", "status": "check"},
                    {"order_type": "profit_stop", "price_scale": "200",
                     "sell_ratio": "100", "status": "check"},
                    {"order_type": "loss_stop", "price_scale": "50",
                     "sell_ratio": "100", "status": "filled"},
                ],
            }],
        })

        pos = {
            "id": 7, "token_symbol": "DOGE", "token_address": "TokDOGE111",
            "paper": 0, "tp1_filled": 0, "tp2_filled": 0, "sl_filled": 0,
        }
        pos_mgr = MagicMock()
        pos_mgr.update_position = AsyncMock()
        pos_mgr.close_position = AsyncMock()

        await _check_one(pos, pos_mgr, gmgn_cli, "WALLET")

        assert pos["sl_filled"] == 1
        pos_mgr.close_position.assert_called_once()
        args, kwargs = pos_mgr.close_position.call_args
        assert kwargs.get("exit_reason") == "SL"

    async def test_no_matching_strategy_skips(self):
        from tracking.strategy_poller import _check_one

        gmgn_cli = MagicMock()
        gmgn_cli.list_strategies = AsyncMock(return_value={"list": []})

        pos = {
            "id": 8, "token_symbol": "X", "token_address": "TokX",
            "paper": 0, "tp1_filled": 0, "tp2_filled": 0, "sl_filled": 0,
        }
        pos_mgr = MagicMock()
        pos_mgr.update_position = AsyncMock()

        await _check_one(pos, pos_mgr, gmgn_cli, "WALLET")

        pos_mgr.update_position.assert_not_called()
