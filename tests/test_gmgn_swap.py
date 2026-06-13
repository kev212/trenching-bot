"""Tests for GMGNCli — subprocess wrapper for official gmgn-cli tool."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

import pytest

from core.gmgn_cli import GMGNCli, SOL_MINT, USDC_MINT


@pytest.fixture
def patch_env_file(tmpdir):
    """Ensure self.env_file.exists() returns True by pointing config_dir to tmpdir."""
    env = tmpdir.join(".env")
    env.write("GMGN_API_KEY=test\nGMGN_PRIVATE_KEY=test")
    return str(tmpdir)


class TestGMGNCliInit:
    def test_default_binary(self):
        cli = GMGNCli(cli_path="gmgn-cli")
        assert cli.cli_path == "gmgn-cli"

    def test_custom_binary(self):
        cli = GMGNCli(cli_path="/usr/local/bin/gmgn-cli")
        assert cli.cli_path == "/usr/local/bin/gmgn-cli"

    @patch("core.gmgn_cli.shutil.which")
    def test_init_not_found_warns(self, mock_which):
        mock_which.return_value = None
        cli = GMGNCli(cli_path="gmgn-cli")
        assert not cli.is_ready()


class TestGMGNCliReady:
    @patch("core.gmgn_cli.shutil.which")
    def test_is_ready_ok(self, mock_which, patch_env_file):
        mock_which.return_value = "/usr/bin/gmgn-cli"
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        assert cli.is_ready()

    @patch("core.gmgn_cli.shutil.which")
    def test_is_ready_missing_binary(self, mock_which, patch_env_file):
        mock_which.return_value = None
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        assert not cli.is_ready()

    @patch("core.gmgn_cli.shutil.which")
    def test_is_ready_missing_env(self, mock_which, tmpdir):
        mock_which.return_value = "/usr/bin/gmgn-cli"
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=str(tmpdir))
        assert not cli.is_ready()


class TestGMGNCliQuote:
    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_quote_success(self, mock_run, patch_env_file):
        mock_run.return_value = {
            "out_amount": 999000,
            "price_impact_pct": 0.5,
        }
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.quote(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000,
        )
        assert result["out_amount"] == 999000
        mock_run.assert_called_once()

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_quote_default_slippage(self, mock_run, patch_env_file):
        mock_run.return_value = {"out_amount": 999000}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.quote(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000,
        )
        args = mock_run.call_args[0][0]
        assert "--slippage" in args
        idx = args.index("--slippage")
        assert args[idx + 1] == "30"

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_quote_custom_slippage(self, mock_run, patch_env_file):
        mock_run.return_value = {"out_amount": 999000}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.quote(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000, slippage=500,
        )
        args = mock_run.call_args[0][0]
        idx = args.index("--slippage")
        assert args[idx + 1] == "500"


class TestGMGNCliSwap:
    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_swap_success(self, mock_run, patch_env_file):
        mock_run.return_value = {"order_id": "ord_123"}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.swap(
            chain="sol", from_addr="TestPubkey",
            input_token=USDC_MINT, output_token=SOL_MINT,
            amount=1_000_000,
        )
        assert result["order_id"] == "ord_123"

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_swap_with_anti_mev(self, mock_run, patch_env_file):
        mock_run.return_value = {"order_id": "ord_456"}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.swap(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000, anti_mev=True,
        )
        args = mock_run.call_args[0][0]
        assert "--anti-mev" in args

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_swap_without_anti_mev(self, mock_run, patch_env_file):
        mock_run.return_value = {"order_id": "ord_789"}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.swap(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000, anti_mev=False,
        )
        args = mock_run.call_args[0][0]
        assert "--anti-mev" not in args

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_swap_priority_fee(self, mock_run, patch_env_file):
        mock_run.return_value = {"order_id": "ord_abc"}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.swap(
            chain="sol", from_addr="TestPubkey",
            input_token=SOL_MINT, output_token=USDC_MINT,
            amount=1_000_000, priority_fee=0.001,
        )
        args = mock_run.call_args[0][0]
        idx = args.index("--priority-fee")
        assert args[idx + 1] == "0.001"


class TestGMGNCliGetOrder:
    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_get_order_status(self, mock_run, patch_env_file):
        mock_run.return_value = {"status": "Filled", "filled_amount": "1000000"}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.get_order("sol", "ord_123")
        assert result["status"] == "Filled"

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_get_order_empty(self, mock_run, patch_env_file):
        mock_run.return_value = {"status": ""}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.get_order("sol", "ord_999")
        assert result["status"] == ""


class TestGMGNCliWaitForOrder:
    @patch("core.gmgn_cli.GMGNCli.get_order", new_callable=AsyncMock)
    async def test_wait_filled(self, mock_get_order, patch_env_file):
        mock_get_order.return_value = {
            "order_id": "ord_123",
            "confirmation": {"state": "confirmed", "detail": "success"},
        }
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.wait_for_order("sol", "ord_123")
        assert result["confirmation"]["state"] == "confirmed"
        mock_get_order.assert_called_once()

    @patch("core.gmgn_cli.GMGNCli.get_order", new_callable=AsyncMock)
    async def test_wait_polls_til_filled(self, mock_get_order, patch_env_file):
        mock_get_order.side_effect = [
            {"confirmation": {"state": "processed"}},
            {"confirmation": {"state": "processed"}},
            {"confirmation": {"state": "confirmed", "detail": "success"}},
        ]
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.wait_for_order(
            "sol", "ord_123", poll_interval_s=0.01,
        )
        assert result["confirmation"]["state"] == "confirmed"
        assert mock_get_order.call_count == 3

    @patch("core.gmgn_cli.GMGNCli.get_order", new_callable=AsyncMock)
    async def test_wait_timeout(self, mock_get_order, patch_env_file):
        mock_get_order.return_value = {"confirmation": {"state": "processed"}}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.wait_for_order(
            "sol", "ord_123", timeout_s=0.05, poll_interval_s=0.01,
        )
        assert result["confirmation"]["state"] == "processed"

    @patch("core.gmgn_cli.GMGNCli.get_order", new_callable=AsyncMock)
    async def test_wait_failed(self, mock_get_order, patch_env_file):
        mock_get_order.side_effect = [
            {"confirmation": {"state": "processed"}},
            {"confirmation": {"state": "failed"}, "error_code": "SLIPPAGE_EXCEEDED"},
        ]
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.wait_for_order(
            "sol", "ord_123", timeout_s=5, poll_interval_s=0.01,
        )
        assert result["confirmation"]["state"] == "failed"

    @patch("core.gmgn_cli.GMGNCli.get_order", new_callable=AsyncMock)
    async def test_wait_expired(self, mock_get_order, patch_env_file):
        mock_get_order.return_value = {"confirmation": {"state": "expired"}}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.wait_for_order("sol", "ord_123", timeout_s=5)
        assert result["confirmation"]["state"] == "expired"


class TestGMGNCliTerminal:
    def test_confirmation_state_terminal(self):
        assert GMGNCli._is_terminal({"confirmation": {"state": "confirmed"}})
        assert GMGNCli._is_terminal({"confirmation": {"state": "failed"}})
        assert GMGNCli._is_terminal({"confirmation": {"state": "expired"}})
        assert not GMGNCli._is_terminal({"confirmation": {"state": "processed"}})

    def test_top_level_status_terminal(self):
        assert GMGNCli._is_terminal({"status": "confirmed"})
        assert GMGNCli._is_terminal({"status": "failed"})
        assert GMGNCli._is_terminal({"status": "expired"})
        assert not GMGNCli._is_terminal({"status": "processed"})
        assert not GMGNCli._is_terminal({"status": "pending"})
        assert not GMGNCli._is_terminal({})
        assert not GMGNCli._is_terminal({"confirmation": {}})


class TestGMGNCliGasPrice:
    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_gas_price(self, mock_run, patch_env_file):
        mock_run.return_value = {"gas_price": 1000, "priority_fee": 500}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli.gas_price("sol")
        assert result["gas_price"] == 1000


class TestGMGNCliListStrategies:
    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_list_strategies(self, mock_run, patch_env_file):
        mock_run.return_value = {"list": [], "total": 0}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.list_strategies(
            chain="sol", from_addr="WALLET", base_token="TOKEN",
        )
        args = mock_run.call_args[0][0]
        assert "strategy" in args
        assert "list" in args
        assert "--group-tag" in args
        idx = args.index("--group-tag")
        assert args[idx + 1] == "STMix"
        assert "--base-token" in args

    @patch("core.gmgn_cli.GMGNCli._run")
    async def test_list_strategies_no_base_token(self, mock_run, patch_env_file):
        mock_run.return_value = {"list": []}
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        await cli.list_strategies(chain="sol", from_addr="WALLET")
        args = mock_run.call_args[0][0]
        assert "--base-token" not in args


class TestGMGNCliRun:
    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_run_returns_parsed_json(self, mock_exec, patch_env_file):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(
            return_value=(json.dumps({"key": "value"}).encode(), b"")
        )
        mock_exec.return_value = mock_process
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli._run(["quote", "--help"])
        assert result == {"key": "value"}

    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_run_nonzero_exit(self, mock_exec, patch_env_file):
        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(
            return_value=(b"{}", b"something went wrong")
        )
        mock_exec.return_value = mock_process
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli._run(["quote"])
        assert result == {}

    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_run_empty_stdout(self, mock_exec, patch_env_file):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec.return_value = mock_process
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli._run(["quote"])
        assert result == {}

    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_run_file_not_found(self, mock_exec, patch_env_file):
        mock_exec.side_effect = FileNotFoundError("no binary")
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli._run(["quote"])
        assert result == {}

    @patch("core.gmgn_cli.asyncio.create_subprocess_exec")
    async def test_run_non_json_stdout(self, mock_exec, patch_env_file):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(
            return_value=(b"not json at all", b"")
        )
        mock_exec.return_value = mock_process
        cli = GMGNCli(cli_path="gmgn-cli", config_dir=patch_env_file)
        result = await cli._run(["quote"])
        assert result == {"_raw": "not json at all"}


class TestGMGNCliSolMints:
    def test_sol_mint(self):
        assert SOL_MINT == "So11111111111111111111111111111111111111112"

    def test_usdc_mint(self):
        assert USDC_MINT == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
