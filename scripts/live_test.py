#!/usr/bin/env python3
"""Standalone E2E test: SOL → USDC → SOL round-trip via GMGN swap.

Usage:
    # Dry-run (validate auth, no actual swap)
    python scripts/live_test.py --dry-run

    # Live round-trip (requires env vars, wallet with SOL)
    python scripts/live_test.py

    # Custom amount
    python scripts/live_test.py --amount 0.005

Requires env vars (in .env or shell):
    GMGN_API_KEY, GMGN_PRIVATE_KEY, WALLET_PRIVATE_KEY,
    WALLET_PUBKEY, HELIUS_API_KEY, HTTP_PROXY (optional)
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from project root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.gmgn_swap import GMGNSwapClient, SOL_MINT, USDC_MINT
from core.wallet import Wallet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("live_test")

RESERVE_FLOOR_SOL = 0.01


async def run_test(dry_run: bool = False, amount_sol: float = 0.001):
    api_key = os.environ.get("GMGN_API_KEY", "")
    gmgn_pem = os.environ.get("GMGN_PRIVATE_KEY", "")
    wallet_b58 = os.environ.get("WALLET_PRIVATE_KEY", "")
    wallet_pubkey = os.environ.get("WALLET_PUBKEY", "")
    helius_key = os.environ.get("HELIUS_API_KEY", "")
    proxy = os.environ.get("HTTP_PROXY", "")

    if not all([api_key, gmgn_pem, wallet_b58, wallet_pubkey, helius_key]):
        logger.error(
            "Missing env vars. Required: "
            "GMGN_API_KEY, GMGN_PRIVATE_KEY, WALLET_PRIVATE_KEY, "
            "WALLET_PUBKEY, HELIUS_API_KEY"
        )
        return 1

    wallet = Wallet(
        paper=False,
        private_key_b58=wallet_b58,
        helius_api_key=helius_key,
    )
    gmgn_swap = GMGNSwapClient(
        api_key=api_key,
        private_key_pem=gmgn_pem,
        wallet_pubkey=wallet_pubkey,
        proxy=proxy,
    )
    await gmgn_swap.start()

    logger.info("=" * 50)
    logger.info("LIVE TEST: SOL → USDC → SOL")
    logger.info(f"Wallet: {wallet_pubkey[:12]}...")
    logger.info(f"Amount: {amount_sol} SOL")
    logger.info(f"Dry-run: {dry_run}")
    logger.info("=" * 50)

    balance_before = await wallet.get_sol_balance()
    logger.info(f"SOL balance before: {balance_before:.6f}")

    if balance_before < amount_sol + RESERVE_FLOOR_SOL:
        logger.error(
            f"Insufficient balance: {balance_before:.6f} SOL "
            f"(need {amount_sol + RESERVE_FLOOR_SOL:.6f})"
        )
        await gmgn_swap.close()
        return 1

    if not gmgn_swap.is_ready():
        logger.error("GMGNSwapClient not ready (Ed25519 key not loaded)")
        await gmgn_swap.close()
        return 1

    if dry_run:
        logger.info("[DRY-RUN] Get quote: SOL → USDC...")
        quote = await gmgn_swap.get_quote(
            token_in=SOL_MINT,
            token_out=USDC_MINT,
            amount=int(amount_sol * 1e9),
        )
        if quote:
            logger.info(f"[DRY-RUN] Quote: {quote}")
        else:
            logger.warning("[DRY-RUN] Quote unavailable (may need auth)")

        logger.info("[DRY-RUN] Skipping actual swap.")
        await gmgn_swap.close()
        logger.info("[DRY-RUN] All checks passed. Ready for live test.")
        return 0

    # Step 1: SOL → USDC
    logger.info(f"\n--- Step 1: SOL → USDC ({amount_sol} SOL) ---")
    amount_lamports = int(amount_sol * 1e9)
    result1 = await gmgn_swap.swap(
        token_in=SOL_MINT,
        token_out=USDC_MINT,
        amount=amount_lamports,
        slippage_bps=300,
    )
    if not result1 or not result1.get("tx_id"):
        logger.error("Step 1 failed: no tx_id returned")
        await gmgn_swap.close()
        return 1

    tx1 = result1["tx_id"]
    out_amount = float(result1.get("out_amount", 0))
    usdc_received = out_amount / 1e6  # USDC has 6 decimals
    logger.info(f"SOL → USDC: tx={tx1}")
    logger.info(f"USDC received: {usdc_received:.6f}")

    if usdc_received <= 0:
        logger.error("Step 1 failed: no USDC received")
        await gmgn_swap.close()
        return 1

    # Wait for confirmation to propagate
    logger.info("Waiting 5s for tx confirmation...")
    await asyncio.sleep(5)

    # Step 2: USDC → SOL
    logger.info(f"\n--- Step 2: USDC → SOL ({usdc_received:.6f} USDC) ---")
    usdc_lamports = int(usdc_received * 1e6)
    result2 = await gmgn_swap.swap(
        token_in=USDC_MINT,
        token_out=SOL_MINT,
        amount=usdc_lamports,
        slippage_bps=300,
    )
    if not result2 or not result2.get("tx_id"):
        logger.error("Step 2 failed: no tx_id returned")
        await gmgn_swap.close()
        return 1

    tx2 = result2["tx_id"]
    out_amount2 = float(result2.get("out_amount", 0))
    sol_received = out_amount2 / 1e9
    logger.info(f"USDC → SOL: tx={tx2}")
    logger.info(f"SOL received: {sol_received:.6f}")

    # Summary
    balance_after = await wallet.get_sol_balance()
    net_change = balance_after - balance_before
    fee_estimate = amount_sol - sol_received

    logger.info("=" * 50)
    logger.info("TEST SUMMARY")
    logger.info(f"  Balance before: {balance_before:.6f} SOL")
    logger.info(f"  Balance after:  {balance_after:.6f} SOL")
    logger.info(f"  Net change:     {net_change:+.6f} SOL")
    logger.info(f"  Round-trip fee: {fee_estimate:.6f} SOL ({fee_estimate/amount_sol*100:.2f}%)")
    logger.info(f"  Tx 1 (SOL→USDC): {tx1}")
    logger.info(f"  Tx 2 (USDC→SOL): {tx2}")
    logger.info("=" * 50)

    if sol_received < amount_sol * 0.9:
        logger.warning(f"High fee: received {sol_received:.6f} SOL from {amount_sol} SOL input")
    else:
        logger.info("Round-trip successful! Fees within normal range.")

    await gmgn_swap.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="GMGN swap E2E test")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate auth without executing swap")
    parser.add_argument("--amount", type=float, default=0.001,
                        help="SOL amount to test (default: 0.001)")
    args = parser.parse_args()
    return asyncio.run(run_test(dry_run=args.dry_run, amount_sol=args.amount))


if __name__ == "__main__":
    sys.exit(main())
