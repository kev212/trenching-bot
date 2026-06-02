"""Unit tests for sources/gmgn.py GMGNClient, focused on /v1/trenches parser.

Run: .venv/bin/python -m pytest tests/test_gmgn_trenches.py -v
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sources.gmgn import GMGNClient


@pytest.fixture
def client():
    return GMGNClient(api_key="test_key_dummy", proxy="")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_get_trenches_returns_items_from_grouped_dict(client):
    """API returns {new_creation: [...], pump: [...], completed: []}."""
    async def _test():
        fake_data = {
            "new_creation": [{"address": "abc123", "symbol": "NEW"}],
            "pump": [],
            "completed": [],
        }
        with patch.object(client, "_post", AsyncMock(return_value=fake_data)):
            result = await client.get_trenches(limit=20)
            assert len(result) == 1
            assert result[0]["address"] == "abc123"
    run(_test())


def test_get_trenches_returns_list_directly(client):
    """API can return a plain list (defensive)."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value=[{"address": "x"}, {"address": "y"}])):
            result = await client.get_trenches(limit=20)
            assert len(result) == 2
    run(_test())


def test_get_trenches_returns_empty_on_empty_dict(client):
    """Free API tier returns empty dict — should yield []."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"new_creation": [], "pump": [], "completed": []})):
            result = await client.get_trenches(limit=20)
            assert result == []
    run(_test())


def test_get_trenches_returns_empty_on_empty_dict_no_keys(client):
    """Empty dict with no expected keys."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={})):
            result = await client.get_trenches(limit=20)
            assert result == []
    run(_test())


def test_get_trenches_returns_empty_on_error(client):
    """_post returns {} on HTTP error."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={})):
            result = await client.get_trenches(limit=20)
            assert result == []
    run(_test())


def test_get_trenches_falls_back_to_pump_when_new_creation_empty(client):
    """If new_creation is empty, fall back to pump."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"new_creation": [], "pump": [{"address": "p1"}], "completed": []})):
            result = await client.get_trenches(limit=20)
            assert len(result) == 1
            assert result[0]["address"] == "p1"
    run(_test())


def test_get_trenches_falls_back_to_completed(client):
    """Last resort: completed key."""
    async def _test():
        with patch.object(client, "_post", AsyncMock(return_value={"new_creation": [], "pump": [], "completed": [{"address": "c1"}]})):
            result = await client.get_trenches(limit=20)
            assert len(result) == 1
            assert result[0]["address"] == "c1"
    run(_test())
