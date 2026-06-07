"""Tests for Telegram Markdown escape + plain-text fallback.

Two layers of defense against HTTP 400 "can't parse entities":

1. alerts.formatter._escape_markdown() escapes user-controlled fields
   (LLM reasoning, key factors, token symbol/name, twitter handle, etc.)
   so the formatter output is always parseable.

2. alerts.dispatcher falls back to plain text when Telegram still rejects
   the message, so an upstream bad field never silently drops the alert.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from alerts.formatter import _escape_markdown, format_alert
from alerts.dispatcher import TelegramDispatcher, _is_parse_error


# --- _escape_markdown unit tests ---------------------------------------------

def test_escape_markdown_escapes_asterisk():
    assert _escape_markdown("this *could* fail") == r"this \*could\* fail"


def test_escape_markdown_escapes_underscore():
    assert _escape_markdown("snake_case_name") == r"snake\_case\_name"


def test_escape_markdown_escapes_backtick():
    assert _escape_markdown("use `code` here") == r"use \`code\` here"


def test_escape_markdown_escapes_bracket():
    assert _escape_markdown("[link] not a link") == r"\[link\] not a link"


def test_escape_markdown_escapes_all_combined():
    text = "token_*name*_with `[all]`"
    out = _escape_markdown(text)
    assert "\\*" in out
    assert "\\_" in out
    assert "\\`" in out
    assert "\\[" in out


def test_escape_markdown_handles_none():
    assert _escape_markdown(None) == ""


def test_escape_markdown_handles_empty():
    assert _escape_markdown("") == ""


def test_escape_markdown_preserves_normal_text():
    # Plain English with no special chars should pass through unchanged
    assert _escape_markdown("This is a normal token name") == "This is a normal token name"


def test_escape_markdown_handles_unclosed_asterisk():
    # The actual production bug: LLM said "this *could be a rug" (unmatched *)
    out = _escape_markdown("this *could be a rug")
    assert "*" not in out.replace("\\*", "") or out.count("\\*") == 1
    # And the result should be parseable: \\* is a literal asterisk
    assert out == r"this \*could be a rug"


# --- _is_parse_error unit tests ----------------------------------------------

def test_is_parse_error_true_on_can_t_parse():
    assert _is_parse_error(400, '"can\'t parse entities"') is True


def test_is_parse_error_true_on_can_t_find_end():
    assert _is_parse_error(400, '"can\'t find end of the entity starting at byte 937"') is True


def test_is_parse_error_false_on_429():
    assert _is_parse_error(429, "Too Many Requests") is False


def test_is_parse_error_false_on_200():
    assert _is_parse_error(200, "OK") is False


def test_is_parse_error_false_on_400_other():
    assert _is_parse_error(400, "Bad Request: chat not found") is False


# --- formatter integration: LLM reasoning with stray Markdown ---------------

def test_format_alert_escapes_llm_reasoning_with_asterisk():
    """LLM reasoning with unmatched * should still produce parseable output."""
    from analysis.models import LLMDecision, TokenData, Verdict
    from datetime import datetime, timezone

    token = TokenData(
        address="So11111111111111111111111111111111111111112",
        symbol="TEST",
        name="Test Token",
        market_cap=100000,
        volume_1h=5000,
        liquidity=10000,
        holders_count=500,
    )
    decision = LLMDecision(
        verdict=Verdict.APE,
        confidence=0.75,
        reasoning="this *could* be a rug, but strong socials",
        key_factors=["*unmatched", "good_community", "[scam?]"],
        score=70,
    )
    msg = format_alert(token, decision, {}, social_score=80.0)
    # The reasoning text should be escaped
    assert r"\*could\*" in msg
    # The key_factors should have * and [ escaped
    assert r"\*unmatched" in msg
    assert r"\[scam?\]" in msg
    assert r"good\_community" in msg


def test_format_alert_escapes_token_symbol():
    """Token symbol with underscore should be escaped."""
    from analysis.models import LLMDecision, TokenData, Verdict

    token = TokenData(
        address="So11111111111111111111111111111111111111112",
        symbol="snake_case",
        name="Test",
        market_cap=1000,
        volume_1h=100,
        liquidity=500,
        holders_count=50,
    )
    decision = LLMDecision(
        verdict=Verdict.SKIP,
        confidence=0.3,
        reasoning="bad token",
        key_factors=[],
        score=20,
    )
    msg = format_alert(token, decision, {}, social_score=10.0)
    # Underscore in symbol must be escaped
    assert r"snake\_case" in msg


# --- dispatcher fallback: parse error -> plain text -------------------------

class _FakeLock:
    """Async context-manager lock that mirrors asyncio.Lock semantics."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _make_dispatcher():
    d = TelegramDispatcher.__new__(TelegramDispatcher)
    d.token = "test_token"
    d.chat_id = "test_chat"
    d._session = None
    # Use a fake lock — real asyncio.Lock() requires a running event loop,
    # but we test send_message in isolation via new_event_loop().
    d._send_lock = _FakeLock()
    return d


def test_dispatcher_falls_back_to_plain_text_on_parse_error():
    """HTTP 400 with 'can't parse entities' -> retry as plain text -> success."""
    d = _make_dispatcher()
    call_log = []

    async def fake_post(text, parse_mode):
        call_log.append(parse_mode)
        if parse_mode == "Markdown":
            return (False, 400, '{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities: ..."}')
        return (True, 200, '{"ok":true}')

    d._post_once = fake_post

    async def _run():
        return await d.send_message("hello *world")

    ok = asyncio.new_event_loop().run_until_complete(_run())
    assert ok is True
    # First call was Markdown (failed), second was plain text (succeeded)
    assert call_log == ["Markdown", ""]


def test_dispatcher_does_not_loop_fallback_infinitely():
    """If plain text also fails, dispatcher should not retry Markdown."""
    d = _make_dispatcher()
    call_log = []

    async def fake_post(text, parse_mode):
        call_log.append(parse_mode)
        # Realistic Telegram parse-error body that triggers our fallback
        return (False, 400, '"description":"Bad Request: can\'t parse entities"')

    d._post_once = fake_post

    async def _run():
        return await d.send_message("hello")

    ok = asyncio.new_event_loop().run_until_complete(_run())
    assert ok is False
    # Markdown tried ONCE (then fallback to plain text), then plain text 2 more times
    markdown_calls = call_log.count("Markdown")
    plain_calls = call_log.count("")
    assert markdown_calls == 1, f"expected 1 Markdown call, got {markdown_calls}: {call_log}"
    assert plain_calls == 2, f"expected 2 plain-text calls, got {plain_calls}: {call_log}"


def test_dispatcher_preserves_markdown_on_success():
    """Happy path: message sent successfully on first try with Markdown."""
    d = _make_dispatcher()
    call_log = []

    async def fake_post(text, parse_mode):
        call_log.append(parse_mode)
        return (True, 200, '{"ok":true}')

    d._post_once = fake_post

    async def _run():
        return await d.send_message("hello")

    ok = asyncio.new_event_loop().run_until_complete(_run())
    assert ok is True
    # Single call, with Markdown
    assert call_log == ["Markdown"]


def test_dispatcher_retries_transient_errors():
    """HTTP 500 (transient) should retry 3 times with backoff, not fallback."""
    d = _make_dispatcher()
    call_log = []

    async def fake_post(text, parse_mode):
        call_log.append(parse_mode)
        return (False, 500, "server error")

    d._post_once = fake_post

    async def _run():
        return await d.send_message("hello")

    ok = asyncio.new_event_loop().run_until_complete(_run())
    assert ok is False
    # All 3 attempts use Markdown (not a parse error, no fallback)
    assert call_log == ["Markdown", "Markdown", "Markdown"]
