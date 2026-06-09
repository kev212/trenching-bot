"""Single wallet abstraction. Paper mode = no real key needed.

In paper mode, the wallet simulates SOL balance and tracks debits/credits
locally. The pubkey is a fixed placeholder string. Real signing, RPC
submission, and Fernet-encrypted key loading are deferred to Phase 2.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Union

from storage.database import Database

logger = logging.getLogger("wallet")


PAPER_PUBKEY = "PaperWa11et1111111111111111111111111111111111"
RESERVE_SOL = 0.1


class _DummyLock:
    """No-op lock for when no event loop is available (e.g. tests)."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


class Wallet:
    """Single wallet. Paper mode by default."""

    def __init__(self, paper: bool = True, starting_balance_sol: float = 10.0,
                 db: Optional[Database] = None):
        self.paper = paper
        self._sol_balance = starting_balance_sol if paper else 0.0
        self._pubkey = PAPER_PUBKEY if paper else None
        self._keypair = None
        self._db = db
        # Bug #14 fix: Initialize lock eagerly in __init__. Previously lazy-init
        # in _ensure_lock created a race where two concurrent callers both saw
        # None and created separate Lock instances, losing atomicity.
        # try/except handles the case where no event loop exists (e.g. tests).
        self._lock: Union[asyncio.Lock, "_DummyLock"] = self._make_lock()

    @staticmethod
    def _make_lock() -> Union["asyncio.Lock", "_DummyLock"]:
        """Create an asyncio.Lock, falling back to a no-op lock if no loop."""
        try:
            return asyncio.Lock()
        except RuntimeError:
            return _DummyLock()

    def _ensure_lock(self) -> Union[asyncio.Lock, "_DummyLock"]:
        """Return the lock. Safe under all conditions — lock is always initialized."""
        return self._lock

    @property
    def pubkey(self) -> str:
        return self._pubkey

    async def get_sol_balance(self) -> float:
        if self.paper:
            return self._sol_balance
        return await self._fetch_onchain_balance()

    async def debit(self, amount: float, reason: str) -> bool:
        """Decrease balance. Returns False if insufficient (after reserve)."""
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
        """Generate a fake tx signature for paper mode (audit trail only)."""
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
        """Phase 2: query Helius RPC for SOL balance."""
        raise NotImplementedError("Live mode is Phase 2")

    def _load_encrypted(self):
        """Phase 2: load + decrypt keypair from env."""
        raise NotImplementedError("Live mode is Phase 2")
