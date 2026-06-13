"""Wrapper for the official GMGN CLI tool (`gmgn-cli`).

Uses the official GMGN CLI from npm: https://github.com/GMGNAI/gmgn-skills
The CLI handles:
  - Ed25519 signed auth
  - Cloudflare bypass
  - Rate limiting
  - Transaction submission
  - Order polling

Credentials live in ~/.config/gmgn/.env (read by the CLI itself, not by
this wrapper). Required:
  - GMGN_API_KEY
  - GMGN_PRIVATE_KEY (Ed25519 PEM)

Install:
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt install -y nodejs
  sudo npm install -g gmgn-cli

First-time setup:
  mkdir -p ~/.config/gmgn
  cat > ~/.config/gmgn/.env <<EOF
  GMGN_API_KEY=<key>
  GMGN_PRIVATE_KEY="<pem content>"
  EOF
  chmod 600 ~/.config/gmgn/.env
"""
import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gmgn_cli")

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_DECIMALS = 9

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 60.0


class GMGNCliError(Exception):
    """Raised when gmgn-cli returns a non-zero exit code or invalid JSON."""


class GMGNCli:
    """Subprocess wrapper for the gmgn-cli tool.

    All async methods return parsed JSON dicts on success, or {} on
    failure (with error logged).
    """

    def __init__(self, cli_path: str = "gmgn-cli",
                 config_dir: str = "~/.config/gmgn",
                 env: Optional[dict] = None):
        self.cli_path = cli_path
        self.config_dir = Path(os.path.expanduser(config_dir))
        self.env_file = self.config_dir / ".env"
        self._env = env or os.environ.copy()
        self._check_cli()

    def _check_cli(self) -> None:
        if not shutil.which(self.cli_path):
            logger.warning(
                f"[GMGN-CLI] binary not found at '{self.cli_path}'. "
                f"Install via: sudo npm install -g gmgn-cli"
            )

    def is_ready(self) -> bool:
        """Check if CLI is installed and credentials file exists."""
        return shutil.which(self.cli_path) is not None and self.env_file.exists()

    async def _run(self, args: list) -> dict:
        """Run gmgn-cli with the given args, return parsed JSON or {}."""
        full_env = {**self._env}
        if full_env.get("GMGN_API_KEY") is None:
            full_env["GMGN_API_KEY"] = ""
        if full_env.get("GMGN_PRIVATE_KEY") is None:
            full_env["GMGN_PRIVATE_KEY"] = ""
        cmd = [self.cli_path, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError as e:
            logger.error(f"[GMGN-CLI] command not found: {e}")
            return {}
        except Exception as e:
            logger.error(f"[GMGN-CLI] subprocess error: {e}")
            return {}

        out = stdout.decode().strip() if stdout else ""
        err = stderr.decode().strip() if stderr else ""

        if proc.returncode != 0:
            logger.warning(
                f"[GMGN-CLI] exit={proc.returncode} args={args[:3]}... "
                f"err={err[:200]}"
            )
            return {}

        if not out:
            return {}

        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"_raw": out}

    async def quote(self, chain: str, from_addr: str,
                    input_token: str, output_token: str,
                    amount: int, slippage: int = 30) -> dict:
        """Get a swap quote (no transaction submitted)."""
        return await self._run([
            "order", "quote",
            "--chain", chain,
            "--from", from_addr,
            "--input-token", input_token,
            "--output-token", output_token,
            "--amount", str(amount),
            "--slippage", str(slippage),
        ])

    async def swap(self, chain: str, from_addr: str,
                   input_token: str, output_token: str,
                   amount: int, slippage: int = 30,
                   anti_mev: bool = True,
                   condition_orders: Optional[str] = None,
                   priority_fee: Optional[float] = None,
                   tip_fee: Optional[float] = None) -> dict:
        """Submit a swap. Returns dict with order_id, hash, status."""
        args = [
            "swap",
            "--chain", chain,
            "--from", from_addr,
            "--input-token", input_token,
            "--output-token", output_token,
            "--amount", str(amount),
            "--slippage", str(slippage),
        ]
        if anti_mev:
            args.append("--anti-mev")
        if condition_orders:
            args.extend(["--condition-orders", condition_orders])
        if priority_fee is not None:
            args.extend(["--priority-fee", str(priority_fee)])
        if tip_fee is not None:
            args.extend(["--tip-fee", str(tip_fee)])
        return await self._run(args)

    async def get_order(self, chain: str, order_id: str) -> dict:
        """Poll an order by ID. Returns status dict."""
        return await self._run([
            "order", "get",
            "--chain", chain,
            "--order-id", order_id,
        ])

    async def wait_for_order(self, chain: str, order_id: str,
                              timeout_s: float = POLL_TIMEOUT_S,
                              poll_interval_s: float = POLL_INTERVAL_S) -> dict:
        """Poll order until status is confirmed/failed/expired or timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        status: dict = {}
        while loop.time() < deadline:
            status = await self.get_order(chain, order_id)
            if self._is_terminal(status):
                return status
            await asyncio.sleep(poll_interval_s)
        logger.warning(
            f"[GMGN-CLI] order {order_id[:16]} did not confirm within {timeout_s}s"
        )
        return status

    @staticmethod
    def _is_terminal(status: dict) -> bool:
        conf = status.get("confirmation", {})
        if isinstance(conf, dict):
            state = conf.get("state", "")
            if state in ("confirmed", "failed", "expired"):
                return True
        state = status.get("status", "")
        return state in ("confirmed", "failed", "expired")

    async def portfolio_info(self) -> dict:
        """Get wallets and balances bound to the API key."""
        return await self._run(["portfolio", "info"])

    async def get_sol_balance(self) -> float:
        """Get SOL balance of the GMGN hosted Solana wallet. Returns 0.0 on error."""
        info = await self.portfolio_info()
        try:
            for w in info.get("wallets", []):
                if w.get("chain") == "sol":
                    for b in w.get("balances", []):
                        if b.get("symbol") == "SOL":
                            return float(b["balance"])
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[GMGN-CLI] failed to parse SOL balance: {e}")
        return 0.0

    async def get_wallet_address(self, chain: str = "sol") -> str:
        """Get the GMGN hosted wallet address for the given chain."""
        info = await self.portfolio_info()
        try:
            for w in info.get("wallets", []):
                if w.get("chain") == chain:
                    return w["address"]
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[GMGN-CLI] failed to get wallet address for {chain}: {e}")
        return ""

    async def gas_price(self, chain: str) -> dict:
        """Get recommended gas price tiers (API-key-only, no signed auth)."""
        return await self._run(["gas-price", "--chain", chain])
