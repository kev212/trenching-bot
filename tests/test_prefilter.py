"""Unit tests for main.py TrenchingBot._passes_prefilter.

Run: .venv/bin/python -m pytest tests/test_prefilter.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
config.settings.paper_mode = True  # avoid paper-mode wallet init in old import

import main


class FakeBot(main.TrenchingBot):
    def __init__(self):
        self.state = MagicMock()
        self.state.metrics = MagicMock()


@pytest.fixture
def bot():
    return FakeBot()


PASS = {"address": "abc123", "symbol": "TEST", "market_cap": 50000, "holder_count": 400, "is_wash_trading": False}


def test_prefilter_passes_good_token(bot):
    assert bot._passes_prefilter(PASS) is True


def test_prefilter_rejects_mc_zero(bot):
    assert bot._passes_prefilter(dict(PASS, market_cap=0)) is False


def test_prefilter_rejects_mc_below_7k(bot):
    assert bot._passes_prefilter(dict(PASS, market_cap=5000)) is False


def test_prefilter_rejects_mc_above_200k(bot):
    assert bot._passes_prefilter(dict(PASS, market_cap=300000)) is False


def test_prefilter_rejects_few_holders(bot):
    assert bot._passes_prefilter(dict(PASS, holder_count=50)) is False


def test_prefilter_rejects_wash_trading(bot):
    assert bot._passes_prefilter(dict(PASS, is_wash_trading=True)) is False


def test_prefilter_records_metric_on_reject(bot):
    bot._passes_prefilter(dict(PASS, market_cap=0))
    bot.state.metrics.record_call.assert_called_with("SKIP")


def test_prefilter_uses_zero_defaults(bot):
    assert bot._passes_prefilter({"address": "abc"}) is False  # mc=0
