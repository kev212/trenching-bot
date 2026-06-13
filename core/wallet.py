"""Single wallet abstraction. Paper mode = no real key needed.

In paper mode, the wallet simulates SOL balance and tracks debits/credits
locally. The pubkey is a fixed placeholder string.

In live mode, the wallet loads a real Solana keypair from a base58-encoded
private key (WALLET_PRIVATE_KEY) and fetches on-chain balances via Helius RPC.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Union

import aiohttp

logger = logging.getLogger("wallet")

PAPER_PUBKEY = "PaperWa11et1111111111111111111111111111111111"
RESERVE_SOL = 0.1

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com"


class _DummyLock:
    """No-op lock for when no event loop is available (e.g. tests)."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


class Wallet:
    """Single wallet. Paper mode by default."""

    def __init__(self, paper: bool = True, starting_balance_sol: float = 10.0,
                 db=None, private_key_b58: str = "",
                 helius_api_key: str = "", helius_rpc_url: str = ""):
        self.paper = paper
        self._sol_balance = starting_balance_sol if paper else 0.0
        self._pubkey = PAPER_PUBKEY if paper else None
        self._keypair = None
        self._db = db
        self._helius_api_key = helius_api_key
        self._helius_rpc_url = helius_rpc_url or HELIUS_RPC_URL
        self._lock: Union[asyncio.Lock, "_DummyLock"] = self._make_lock()

        if not paper and private_key_b58:
            self._load_base58_keypair(private_key_b58)
            logger.warning(f"[WALLET] Live mode: pubkey={self._pubkey[:12]}...")

    @staticmethod
    def _make_lock() -> Union["asyncio.Lock", "_DummyLock"]:
        try:
            return asyncio.Lock()
        except RuntimeError:
            return _DummyLock()

    def _ensure_lock(self) -> Union[asyncio.Lock, "_DummyLock"]:
        return self._lock

    def _load_base58_keypair(self, b58_key: str):
        """Load Solana keypair from base58-encoded private key."""
        from solders.keypair import Keypair
        self._keypair = Keypair.from_base58_string(b58_key.strip())
        self._pubkey = str(self._keypair.pubkey())
        logger.info(f"[WALLET] Loaded keypair: pubkey={self._pubkey}")

    @property
    def pubkey(self) -> str:
        return self._pubkey

    @property
    def keypair(self):
        """Return the solders Keypair object (live mode only)."""
        return self._keypair

    async def get_sol_balance(self) -> float:
        if self.paper:
            return self._sol_balance
        return await self._fetch_onchain_balance()

    async def debit(self, amount: float, reason: str) -> bool:
        if amount <= 0:
            return False
        async with self._ensure_lock():
            if amount > self._sol_balance - RESERVE_SOL:
                logger.warning(
                    f"[WALLET] Debit rejected: {amount:.4f} > available "
                    f"{self._sol_balance - RESERVE_SOL:.4f} (reserve {RESERVE_SOL})"
                )
                return False
            self._sol_balance -= amount
            if self._db:
                await self._log_balance()
            logger.info(
                f"[WALLET] Debit {amount:.4f} SOL | "
                f"reason='{reason}' | balance={self._sol_balance:.4f}"
            )
            return True

    async def credit(self, amount: float, reason: str) -> None:
        if amount <= 0:
            return
        async with self._ensure_lock():
            self._sol_balance += amount
            if self._db:
                await self._log_balance()
            logger.info(
                f"[WALLET] Credit {amount:.4f} SOL | "
                f"reason='{reason}' | balance={self._sol_balance:.4f}"
            )

    def generate_paper_signature(self) -> str:
        return f"PAPER_{uuid.uuid4().hex[:16]}"

    async def _log_balance(self) -> None:
        if not self._db or not self._db.db:
            return
        try:
            await self._db.db.execute(
                """INSERT INTO wallet_balances
                (snapshot_time, sol_balance, paper)
                VALUES (?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(),
                 self._sol_balance, 1 if self.paper else 0),
            )
            await self._db.commit()
        except Exception as e:
            logger.debug(f"wallet balance log failed: {e}")

    async def _fetch_onchain_balance(self) -> float:
        """Query Helius RPC for SOL balance of the loaded pubkey."""
        if not self._helius_api_key:
            logger.warning("[WALLET] No Helius API key configured")
            return 0.0
        if not self._pubkey or self._pubkey == PAPER_PUBKEY:
            return 0.0
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [self._pubkey],
        }
        url = f"{self._helius_rpc_url}/?api-key={self._helius_api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"[WALLET] Helius RPC HTTP {resp.status}")
                        return 0.0
                    data = await resp.json()
                    if "result" in data and data["result"] is not None:
                        lamports = data["result"]["value"]
                        balance_sol = lamports / 1e9
                        self._sol_balance = balance_sol
                        logger.info(
                            f"[WALLET] On-chain balance: {balance_sol:.4f} SOL"
                        )
                        return balance_sol
                    logger.warning(f"[WALLET] Helius RPC unexpected response: {data}")
                    return 0.0
        except asyncio.TimeoutError:
            logger.warning("[WALLET] Helius RPC timeout")
            return 0.0
        except Exception as e:
            logger.warning(f"[WALLET] Helius RPC error: {e}")
            return 0.0

    def sign_message(self, message: bytes) -> bytes:
        """Sign a message with the wallet keypair (live mode).

        Returns the 64-byte signature.
        """
        if not self._keypair:
            raise RuntimeError("Cannot sign: no keypair loaded (paper mode)")
        return bytes(self._keypair.sign_message(message))
