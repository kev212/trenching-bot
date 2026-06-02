"""Unit tests for sources/gmgn.py GMGNClient.get_trenches v2 API.

Run: .venv/bin/python -m pytest tests/test_gmgn_trenches.py -v
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sources.gmgn import GMGNClient


@pytest.fixture
def client():
    return GMGNClient(api_key="test_key", proxy="")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_get_trenches_returns_from_pump_key(client):
    """v2 API returns tokens under 'pump' key for near_completion."""
    async def _test():
        fake = {"pump": [{"address": "abc", "symbol": "TEST"}], "new_creation": [], "completed": []}
        with patch.object(client, "_post", AsyncMock(return_value=fake)):
            result = await client.get_trenches(category="near_completion")
            assert len(result) == 1
            assert result[0]["address"] == "abc"
    run(_test())


def test_get_trenches_returns_from_new_creation(client):
    """v2 API returns tokens under 'pump' for new_creation too."""
    async def _test():
        fake = {"pump": [{"address": "x", "symbol": "NEW"}], "new_creation": [], "completed": []}
        with patch.object(client, "_post", AsyncMock(return_value=fake)):
            result = await client.get_trenches(category="new_creation")
            assert len(result) == 1
    run(_test())


def test_get_trenches_returns_empty_on_empty_data(client):
    """Empty data dict yields [].

    The _post method returns the inner data dict directly (after
    unwrapping from the response envelope).
    """
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"pump": [], "new_creation": [], "completed": []})):
            assert await client.get_trenches() == []
    run(_test())


def test_get_trenches_returns_empty_on_no_keys(client):
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={})):
            assert await client.get_trenches() == []
    run(_test())


def test_get_trenches_returns_empty_on_none(client):
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value=None)):
            assert await client.get_trenches() == []
    run(_test())


def test_get_trenches_sends_v2_body(client):
    """Verify the v2 body format is sent to _post."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"pump": []})):
            await client.get_trenches(limit=10, min_smart_degen=2, category="near_completion")
            client._post.assert_called_once()
            args = client._post.call_args[0]
            body = args[2]
            assert body["version"] == "v2"
            assert "near_completion" in body
            assert body["near_completion"]["limit"] == 10
            assert body["near_completion"]["min_smart_degen_count"] == 2
            assert body["near_completion"]["launchpad_platform_v2"] is True
    run(_test())


def test_get_trenches_sends_v2_body_new_creation(client):
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"pump": []})):
            await client.get_trenches(category="new_creation")
            client._post.assert_called_once()
            body = client._post.call_args[0][2]
            assert body["version"] == "v2"
            assert "new_creation" in body
            assert "near_completion" not in body
    run(_test())
