"""GMGN OpenAPI swap client with Ed25519 signed auth.

Uses an Ed25519 PEM key (GMGN_PRIVATE_KEY) to sign swap API requests.
The wallet keypair (WALLET_PRIVATE_KEY, base58) is the source of funds.

USDC mint: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
SOL mint (wrapped): So11111111111111111111111111111111111111112
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger("gmgn_swap")

SWAP_BASE_URL = "https://openapi.gmgn.ai"
SWAP_TIMEOUT = 30

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9


class _DummyLock:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


class SignatureError(Exception):
    """Raised when Ed25519 signing fails."""


class GMGNSwapClient:
    """GMGN OpenAPI swap client with Ed25519 signed auth.

    Handles swap execution via GMGN's DeFi API. Requires:
      - api_key: GMGN API key (same as GMGNClient)
      - private_key_pem: Ed25519 private key in PEM format for signing
      - wallet_pubkey: Solana wallet pubkey (source of funds)
      - proxy: Optional HTTP proxy
    """

    def __init__(self, api_key: str, private_key_pem: str,
                 wallet_pubkey: str, proxy: str = ""):
        self.api_key = api_key
        self.wallet_pubkey = wallet_pubkey
        self.proxy = proxy
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = self._make_lock()
        self._private_key = None
        if private_key_pem:
            self._private_key = self._load_ed25519(private_key_pem)

    @staticmethod
    def _make_lock():
        try:
            return asyncio.Lock()
        except RuntimeError:
            return _DummyLock()

    @staticmethod
    def _load_ed25519(pem_or_path: str):
        """Load Ed25519 private key from PEM string or file path.

        If the string is an existing file path, reads from file.
        Otherwise treats as raw PEM content (supports \\n escape sequences).
        """
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519

            # Check if it's a file path
            if os.path.isfile(pem_or_path):
                key_bytes = Path(pem_or_path).read_bytes()
            else:
                key_bytes = pem_or_path.encode() if isinstance(pem_or_path, str) else pem_or_path
                # Fix literal \n from .env files (convert "\\n" to actual newlines)
                if b"\\n" in key_bytes:
                    key_bytes = key_bytes.replace(b"\\n", b"\n")
                # Strip surrounding quotes if present
                key_bytes = key_bytes.strip().strip(b"\"").strip(b"'")

            return serialization.load_pem_private_key(key_bytes, password=None)
        except Exception as e:
            raise SignatureError(f"Failed to load Ed25519 key: {e}") from e

    def is_ready(self) -> bool:
        """Check if the client has a loaded private key for signing."""
        return self._private_key is not None

    def _sign(self, payload: str) -> str:
        """Sign payload with Ed25519, return hex-encoded signature."""
        if not self._private_key:
            raise SignatureError("Ed25519 private key not loaded")
        sig = self._private_key.sign(payload.encode())
        return sig.hex()

    def _make_signature_payload(self, body: dict) -> str:
        sorted_items = sorted(body.items(), key=lambda x: x[0])
        return "&".join(f"{k}={v}" for k, v in sorted_items)

    def _sign_body(self, body: dict) -> tuple[str, str]:
        ts = str(int(time.time() * 1000))
        body_with_ts = {**body, "timestamp": ts}
        canonical = self._make_signature_payload(body_with_ts)
        sig = self._sign(canonical)
        return sig, ts

    async def start(self):
        async with self._session_lock:
            if self._session is None:
                timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT)
                kwargs = {"timeout": timeout}
                if self.proxy:
                    kwargs["proxy"] = self.proxy
                self._session = aiohttp.ClientSession(**kwargs)

    async def close(self):
        async with self._session_lock:
            if self._session:
                await self._session.close()
                self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None:
                timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT)
                kwargs = {"timeout": timeout}
                if self.proxy:
                    kwargs["proxy"] = self.proxy
                self._session = aiohttp.ClientSession(**kwargs)
            return self._session

    async def swap(
        self,
        token_in: str,
        token_out: str,
        amount: int,
        slippage_bps: int = 300,
        chain: str = "sol",
    ) -> dict:
        """Execute a swap via GMGN OpenAPI.

        Args:
            token_in: Input token mint address (SOL_MINT for SOL).
            token_out: Output token mint address.
            amount: Amount in smallest units (lamports for SOL).
            slippage_bps: Slippage in basis points (300 = 3%).
            chain: Blockchain identifier.

        Returns:
            Dict with keys 'tx_id', 'out_amount', 'fee' on success, or {}.
        """
        if not self.is_ready():
            logger.error("[GMGN-SWAP] cannot swap: Ed25519 key not loaded")
            return {}

        body = {
            "chain": chain,
            "from": token_in,
            "to": token_out,
            "amount": str(amount),
            "from_address": self.wallet_pubkey,
            "slippage": str(slippage_bps),
        }
        sig, ts = self._sign_body(body)
        headers = {
            "X-APIKEY": self.api_key,
            "X-Signature": sig,
            "X-Timestamp": ts,
            "Content-Type": "application/json",
        }
        session = await self._get_session()
        try:
            async with session.post(
                f"{SWAP_BASE_URL}/defi/v1/tx/swap",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    text = (await resp.text() or "")[:500]
                    logger.warning(f"[GMGN-SWAP] HTTP {resp.status}: {text}")
                    return {}
                data = await resp.json()
                if data.get("code") != 0:
                    logger.warning(
                        f"[GMGN-SWAP] API error: "
                        f"{data.get('msg') or data.get('message') or 'unknown'}"
                    )
                    return {}
                result = data.get("data", {})
                tx_id = result.get("tx_id") or result.get("hash") or ""
                out_amount = result.get("out_amount") or result.get("outAmount", 0)
                fee = result.get("fee", 0)
                if not tx_id:
                    logger.warning(f"[GMGN-SWAP] swap succeeded but no tx_id in response")
                logger.info(
                    f"[GMGN-SWAP] swap {token_in[:8]}->{token_out[:8]} "
                    f"amt={amount} tx={tx_id[:16] if tx_id else '?'} "
                    f"out={out_amount} fee={fee}"
                )
                return {
                    "tx_id": tx_id,
                    "out_amount": out_amount,
                    "fee": fee,
                    "raw": result,
                }
        except asyncio.TimeoutError:
            logger.error("[GMGN-SWAP] swap request timed out")
            return {}
        except Exception as e:
            logger.error(f"[GMGN-SWAP] swap request failed: {e}")
            return {}

    async def get_swap_status(self, tx_id: str, chain: str = "sol") -> dict:
        """Check swap status by transaction ID."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{SWAP_BASE_URL}/defi/v1/tx/status",
                params={"chain": chain, "tx_id": tx_id},
                headers={"X-APIKEY": self.api_key},
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                if data.get("code") != 0:
                    return {}
                return data.get("data", {})
        except Exception as e:
            logger.error(f"[GMGN-SWAP] get_swap_status error: {e}")
            return {}

    async def get_quote(self, token_in: str, token_out: str, amount: int,
                        chain: str = "sol") -> dict:
        """Get a quote for a swap without executing it.

        Useful for estimating output before committing to a trade.
        Returns dict with 'out_amount' and 'price_impact_pct' on success.
        """
        body = {
            "chain": chain,
            "from": token_in,
            "to": token_out,
            "amount": str(amount),
            "from_address": self.wallet_pubkey,
        }
        sig, ts = self._sign_body(body)
        headers = {
            "X-APIKEY": self.api_key,
            "X-Signature": sig,
            "X-Timestamp": ts,
            "Content-Type": "application/json",
        }
        session = await self._get_session()
        try:
            async with session.post(
                f"{SWAP_BASE_URL}/defi/v1/tx/quote",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                if data.get("code") != 0:
                    return {}
                return data.get("data", {})
        except Exception as e:
            logger.error(f"[GMGN-SWAP] get_quote error: {e}")
            return {}
