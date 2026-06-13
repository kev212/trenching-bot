"""Tests for Wallet: paper mode, sync_from_gmgn, debit/credit."""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from core.wallet import Wallet, PAPER_PUBKEY, RESERVE_SOL


class TestWalletPaper:
    def test_paper_default_balance(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)
        assert w.pubkey == PAPER_PUBKEY
        assert w.paper is True

    def test_paper_get_sol_balance(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)
        bal = asyncio.run(w.get_sol_balance())
        assert bal == 10.0

    def test_paper_debit_success(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)

        async def go():
            return await w.debit(0.5, "test")

        assert asyncio.run(go()) is True
        assert w._sol_balance == 9.5

    def test_paper_debit_rejected_below_reserve(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)

        async def go():
            return await w.debit(9.95, "test")

        assert asyncio.run(go()) is False
        assert w._sol_balance == 10.0

    def test_paper_credit(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)

        async def go():
            await w.credit(2.0, "test")

        asyncio.run(go())
        assert w._sol_balance == 12.0

    def test_paper_signature(self):
        w = Wallet(paper=True)
        sig = w.generate_paper_signature()
        assert sig.startswith("PAPER_")
        assert len(sig) > 16


class TestWalletLive:
    def test_live_no_key_no_pubkey(self):
        w = Wallet(paper=False, private_key_b58="")
        assert w.pubkey is None

    def test_live_loads_keypair(self):
        import base58
        from solders.keypair import Keypair
        kp = Keypair()
        b58 = base58.b58encode(bytes(kp)).decode()
        w = Wallet(paper=False, private_key_b58=b58)
        assert w.pubkey == str(kp.pubkey())
        assert w.keypair is not None

    def test_live_get_sol_balance_no_helius_returns_zero(self):
        w = Wallet(paper=False, private_key_b58="", helius_api_key="")
        bal = asyncio.run(w.get_sol_balance())
        assert bal == 0.0


class TestWalletSyncFromGMGN:
    def test_paper_returns_paper_balance(self):
        w = Wallet(paper=True, starting_balance_sol=10.0)
        gmgn = MagicMock()
        bal = asyncio.run(w.sync_from_gmgn(gmgn))
        assert bal == 10.0
        gmgn.get_sol_balance.assert_not_called()

    def test_no_gmgn_cli_returns_current(self):
        w = Wallet(paper=False, private_key_b58="")
        bal = asyncio.run(w.sync_from_gmgn(None))
        assert bal == 0.0

    def test_sync_success_updates_balance(self):
        w = Wallet(paper=False, private_key_b58="")
        gmgn = MagicMock()
        gmgn.get_sol_balance = AsyncMock(return_value=0.5)

        bal = asyncio.run(w.sync_from_gmgn(gmgn))
        assert bal == 0.5
        assert w._sol_balance == 0.5

    def test_sync_zero_balance_keeps_cached(self):
        w = Wallet(paper=False, private_key_b58="")
        w._sol_balance = 0.3
        gmgn = MagicMock()
        gmgn.get_sol_balance = AsyncMock(return_value=0.0)

        bal = asyncio.run(w.sync_from_gmgn(gmgn))
        assert bal == 0.0
        assert w._sol_balance == 0.3

    def test_sync_exception_returns_cached(self):
        w = Wallet(paper=False, private_key_b58="")
        w._sol_balance = 0.7
        gmgn = MagicMock()
        gmgn.get_sol_balance = AsyncMock(side_effect=Exception("network"))

        bal = asyncio.run(w.sync_from_gmgn(gmgn))
        assert bal == 0.7
        assert w._sol_balance == 0.7
