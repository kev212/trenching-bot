"""Tests for GMGNSwapClient — Ed25519 signing and swap API integration."""
import asyncio
import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from sources.gmgn_swap import (
    GMGNSwapClient,
    SignatureError,
    SOL_MINT,
    USDC_MINT,
    SWAP_BASE_URL,
)

# A valid Ed25519 PEM for testing (no real key, just for test signing)
TEST_PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJk7L6R+VSn0sYPq+1v1Gf8G/YMwUed2vTCZqVPoAx/o
-----END PRIVATE KEY-----"""

TEST_API_KEY = "test_api_key_123"
TEST_PUBKEY = "TestPubkey111111111111111111111111111111111111"


def make_client(private_key_pem=TEST_PEM):
    return GMGNSwapClient(
        api_key=TEST_API_KEY,
        private_key_pem=private_key_pem,
        wallet_pubkey=TEST_PUBKEY,
        proxy="",
    )


class MockResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data or {}
        self._text = ""

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    def __init__(self):
        self.post_responses = []
        self.get_responses = []
        self.post_calls = []
        self.get_calls = []

    def add_post_response(self, resp: MockResponse):
        self.post_responses.append(resp)

    def add_get_response(self, resp: MockResponse):
        self.get_responses.append(resp)

    async def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if self.post_responses:
            return self.post_responses.pop(0)
        return MockResponse(200, {"code": 0, "data": {}})

    async def get(self, url, params=None, headers=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        if self.get_responses:
            return self.get_responses.pop(0)
        return MockResponse(200, {"code": 0, "data": {}})

    async def close(self):
        pass


class TestGMGNSwapClientInit:
    def test_load_valid_pem(self):
        client = make_client()
        assert client.is_ready(), "Client should be ready with valid PEM"

    def test_empty_pem_not_ready(self):
        client = make_client(private_key_pem="")
        assert not client.is_ready(), "Client should not be ready without PEM"

    def test_invalid_pem_raises(self):
        try:
            make_client(private_key_pem="not-a-valid-pem")
            assert False, "Should have raised SignatureError"
        except SignatureError:
            pass

    def test_default_values(self):
        client = make_client()
        assert client.api_key == TEST_API_KEY
        assert client.wallet_pubkey == TEST_PUBKEY
        assert client.proxy == ""


class TestGMGNSwapClientSigning:
    def test_sign_produces_hex(self):
        client = make_client()
        sig = client._sign("hello")
        assert isinstance(sig, str)
        assert len(sig) == 128  # 64 bytes = 128 hex chars
        # Same input should produce same signature (deterministic)
        sig2 = client._sign("hello")
        assert sig == sig2

    def test_sign_different_inputs(self):
        client = make_client()
        sig1 = client._sign("message1")
        sig2 = client._sign("message2")
        assert sig1 != sig2

    def test_sign_without_key_raises(self):
        client = make_client(private_key_pem="")
        try:
            client._sign("test")
            assert False, "Should have raised SignatureError"
        except SignatureError:
            pass

    def test_make_signature_payload_sorted(self):
        client = make_client()
        payload = client._make_signature_payload({
            "b": "2",
            "a": "1",
            "c": "3",
        })
        assert payload == "a=1&b=2&c=3"

    def test_make_signature_payload_empty(self):
        client = make_client()
        payload = client._make_signature_payload({})
        assert payload == ""

    def test_sign_body_includes_timestamp(self):
        client = make_client()
        sig, ts = client._sign_body({"chain": "sol", "amount": "100"})
        assert isinstance(sig, str) and len(sig) == 128
        assert isinstance(ts, str)
        # Timestamp should be recent (within 5s)
        assert abs(int(ts) - int(time.time() * 1000)) < 5000


class TestGMGNSwapClientSwap:
    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_swap_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "code": 0,
            "data": {
                "tx_id": "test_tx_123",
                "out_amount": 1000000,
                "fee": 5000,
            },
        })
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = mock_resp

        client = make_client()
        client._session = mock_session

        async def go():
            result = await client.swap(
                token_in=SOL_MINT,
                token_out=USDC_MINT,
                amount=1000000,
                slippage_bps=300,
            )
            assert result["tx_id"] == "test_tx_123"
            assert result["out_amount"] == 1000000
            assert result["fee"] == 5000
            assert "raw" in result
            # Verify headers include auth
            call_headers = mock_session.post.call_args[1]["headers"]
            assert call_headers["X-APIKEY"] == TEST_API_KEY
            assert "X-Signature" in call_headers
            assert "X-Timestamp" in call_headers

        asyncio.run(go())

    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_swap_http_error(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = mock_resp

        client = make_client()
        client._session = mock_session

        async def go():
            result = await client.swap(
                token_in=SOL_MINT,
                token_out=USDC_MINT,
                amount=1000000,
            )
            assert result == {}

        asyncio.run(go())

    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_swap_api_error(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "code": 4001,
            "msg": "Insufficient balance",
        })
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = mock_resp

        client = make_client()
        client._session = mock_session

        async def go():
            result = await client.swap(
                token_in=SOL_MINT,
                token_out=USDC_MINT,
                amount=1000000,
            )
            assert result == {}

        asyncio.run(go())

    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_swap_not_ready(self, mock_session_cls):
        client = make_client(private_key_pem="")
        client._session = MagicMock()

        async def go():
            result = await client.swap(
                token_in=SOL_MINT,
                token_out=USDC_MINT,
                amount=1000000,
            )
            assert result == {}
            # Session.post should NOT be called
            client._session.post.assert_not_called()

        asyncio.run(go())

    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_get_quote(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "code": 0,
            "data": {
                "out_amount": 999000,
                "price_impact_pct": 0.5,
            },
        })
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = mock_resp

        client = make_client()
        client._session = mock_session

        async def go():
            result = await client.get_quote(
                token_in=SOL_MINT,
                token_out=USDC_MINT,
                amount=1000000,
            )
            assert result["out_amount"] == 999000

        asyncio.run(go())

    @patch("sources.gmgn_swap.aiohttp.ClientSession")
    def test_get_swap_status(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "code": 0,
            "data": {
                "status": "confirmed",
                "tx_id": "test_tx_123",
            },
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = mock_resp

        client = make_client()
        client._session = mock_session

        async def go():
            result = await client.get_swap_status("test_tx_123")
            assert result["status"] == "confirmed"

        asyncio.run(go())


class TestGMGNSwapClientSession:
    def test_start_and_close(self):
        client = make_client()

        async def go():
            await client.start()
            assert client._session is not None
            assert not client._session.closed
            await client.close()
            # After close, the session lock should still work
            async with client._session_lock:
                pass

        asyncio.run(go())

    def test_get_session_lazy_init(self):
        client = make_client()

        async def go():
            session = await client._get_session()
            assert session is not None
            await client.close()

        asyncio.run(go())

    def test_is_ready(self):
        assert make_client().is_ready()
        assert not make_client(private_key_pem="").is_ready()
