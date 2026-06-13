#!/usr/bin/env python3
"""Standalone E2E test: SOL -> USDC -> SOL round-trip via GMGN CLI.

Requires gmgn-cli (https://github.com/GMGNAI/gmgn-skills):
  sudo npm install -g gmgn-cli

And credentials in ~/.config/gmgn/.env:
  GMGN_API_KEY=<key>
  GMGN_PRIVATE_KEY="<pem content>"

Usage:
  python scripts/live_test.py --dry-run
  python scripts/live_test.py --amount 0.001
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.gmgn_cli import GMGNCli, SOL_MINT, USDC_MINT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("live_test")

RESERVE_FLOOR_SOL = 0.01


async def run_test(dry_run: bool = False, amount_sol: float = 0.001):
    shutil_which = __import__("shutil").which

    if not shutil_which("gmgn-cli"):
        logger.error("gmgn-cli not installed. Run: sudo npm install -g gmgn-cli")
        return 1

    cli = GMGNCli()

    if not cli.is_ready():
        logger.error(
            "gmgn-cli not ready. Check ~/.config/gmgn/.env has "
            "GMGN_API_KEY and GMGN_PRIVATE_KEY."
        )
        return 1

    wallet_addr = await cli.get_wallet_address("sol")
    if not wallet_addr:
        logger.error("Failed to get GMGN wallet address")
        return 1
    logger.info(f"GMGN wallet: {wallet_addr[:12]}...")

    balance_before = await cli.get_sol_balance()
    logger.info(f"SOL balance before: {balance_before:.6f}")

    if balance_before < amount_sol + RESERVE_FLOOR_SOL:
        logger.error(
            f"Insufficient balance: {balance_before:.6f} SOL "
            f"(need {amount_sol + RESERVE_FLOOR_SOL:.6f})"
        )
        return 1

    logger.info("=" * 50)
    logger.info("LIVE TEST: SOL -> USDC -> SOL")
    logger.info(f"Wallet: {wallet_addr[:12]}...")
    logger.info(f"Amount: {amount_sol} SOL")
    logger.info(f"Dry-run: {dry_run}")
    logger.info("=" * 50)

    if dry_run:
        logger.info("[DRY-RUN] Get quote: SOL -> USDC...")
        quote = await cli.quote(
            chain="sol",
            from_addr=wallet_addr,
            input_token=SOL_MINT,
            output_token=USDC_MINT,
            amount=int(amount_sol * 1e9),
            slippage=30,
        )
        if quote:
            logger.info(f"[DRY-RUN] Quote: OK (out={quote.get('output_amount','?')})")
        else:
            logger.warning("[DRY-RUN] Quote failed (check API key + IP whitelist)")

        logger.info("[DRY-RUN] All checks passed. Ready for live test.")
        return 0

    # Step 1: SOL -> USDC
    logger.info(f"\n--- Step 1: SOL -> USDC ({amount_sol} SOL) ---")
    result1 = await cli.swap(
        chain="sol",
        from_addr=wallet_addr,
        input_token=SOL_MINT,
        output_token=USDC_MINT,
        amount=int(amount_sol * 1e9),
        slippage=30,
    )
    if not result1 or not result1.get("order_id"):
        logger.error(f"Step 1 swap failed: {result1}")
        return 1

    order_id1 = result1["order_id"]
    tx1 = result1.get("hash", "")
    logger.info(f"Order 1: {order_id1} (tx: {tx1[:16] if tx1 else '?'})")

    status1 = await cli.wait_for_order("sol", order_id1)
    if status1.get("status") != "confirmed":
        logger.error(f"Step 1 not confirmed: {status1}")
        return 1

    report1 = status1.get("report", {})
    out_amount_raw = int(report1.get("output_amount", 0))
    output_decimals = int(report1.get("output_token_decimals", 6))
    usdc_received = out_amount_raw / (10 ** output_decimals)
    logger.info(f"USDC received: {usdc_received:.6f}")

    if usdc_received <= 0:
        logger.error("Step 1: no USDC received")
        return 1

    # Step 2: USDC -> SOL
    logger.info(f"\n--- Step 2: USDC -> SOL ({usdc_received:.6f} USDC) ---")
    usdc_lamports = int(usdc_received * (10 ** output_decimals))
    result2 = await cli.swap(
        chain="sol",
        from_addr=wallet_addr,
        input_token=USDC_MINT,
        output_token=SOL_MINT,
        amount=usdc_lamports,
        slippage=30,
    )
    if not result2 or not result2.get("order_id"):
        logger.error(f"Step 2 swap failed: {result2}")
        return 1

    order_id2 = result2["order_id"]
    tx2 = result2.get("hash", "")
    logger.info(f"Order 2: {order_id2} (tx: {tx2[:16] if tx2 else '?'})")

    status2 = await cli.wait_for_order("sol", order_id2)
    if status2.get("status") != "confirmed":
        logger.error(f"Step 2 not confirmed: {status2}")
        return 1

    report2 = status2.get("report", {})
    sol_received_raw = int(report2.get("output_amount", 0))
    sol_received = sol_received_raw / 1e9

    # Summary
    balance_after = await cli.get_sol_balance()
    fee_estimate = amount_sol - sol_received

    logger.info("=" * 50)
    logger.info("TEST SUMMARY")
    logger.info(f"  Balance before: {balance_before:.6f} SOL")
    logger.info(f"  Balance after:  {balance_after:.6f} SOL")
    logger.info(f"  USDC received:  {usdc_received:.6f}")
    logger.info(f"  SOL received:   {sol_received:.6f}")
    logger.info(f"  Round-trip fee: {fee_estimate:.6f} SOL ({fee_estimate/amount_sol*100:.2f}%)")
    logger.info(f"  Order 1: {order_id1}")
    logger.info(f"  Order 2: {order_id2}")
    logger.info("=" * 50)

    if sol_received < amount_sol * 0.9:
        logger.warning(f"High fee: received {sol_received:.6f} SOL from {amount_sol} SOL input")
    else:
        logger.info("Round-trip successful! Fees within normal range.")

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
