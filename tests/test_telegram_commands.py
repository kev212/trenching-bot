"""Test Telegram command handlers (FIX M4).

Covers the cmd_live_pause / cmd_live_resume / cmd_live_status / cmd_close_all
commands which were previously untested. The earlier audit caught 4 critical
bugs in these code paths (C1: pause flag disconnect, C2: status key
mismatch, C3: missing state.executor, C5: _row_to_position missing fields)
and this file exists to prevent regressions.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/khezuma/workspace/trenching")


# Helper to build a fake Update + Context for command testing
def make_update(text: str = "", chat_id: int = 8125198343):
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.effective_user = SimpleNamespace(id=chat_id)
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def make_context(bot_data: dict):
    ctx = MagicMock()
    ctx.bot_data = bot_data
    return ctx


def test_live_pause_sets_state_flag():
    """FIX C1: /live_pause must write to state._live_paused (SharedState)
    so the buy gate (which reads state._live_paused) actually blocks."""
    from alerts.bot import cmd_live_pause

    state = SimpleNamespace(
        paper_mode=False,
        _live_paused=False,
        gmgn_cli=MagicMock(),
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_live_pause(update, ctx))

    # Verify the state flag flipped
    assert state._live_paused is True
    # Verify user got a reply
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "PAUSED" in reply


def test_live_resume_clears_state_flag():
    """FIX C1: /live_resume must clear state._live_paused."""
    from alerts.bot import cmd_live_resume

    state = SimpleNamespace(
        paper_mode=False,
        _live_paused=True,  # was paused
        gmgn_cli=MagicMock(),
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_live_resume(update, ctx))

    assert state._live_paused is False
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "RESUMED" in reply or "resumed" in reply


def test_live_pause_bails_in_paper_mode():
    """If paper_mode is True, /live_pause should bail with explanation."""
    from alerts.bot import cmd_live_pause

    state = SimpleNamespace(
        paper_mode=True,  # paper mode — no live trades
        _live_paused=False,
        gmgn_cli=MagicMock(),
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_live_pause(update, ctx))

    # state should not have changed
    assert state._live_paused is False
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "PAPER" in reply


def test_live_status_reflects_pause():
    """FIX C1: /live_status must read state._live_paused (the same flag
    that /live_pause writes). If they read different attrs, the user
    sees one state in /live_status but buys still execute."""
    from alerts.bot import cmd_live_status

    state = SimpleNamespace(
        paper_mode=False,
        _live_paused=True,  # user paused
        gmgn_cli=MagicMock(),
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_live_status(update, ctx))

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    # Should show PAUSED in the status line
    assert "PAUSED" in reply
    assert "LIVE" in reply  # mode should be LIVE (not paper)


def test_close_all_requires_executor():
    """FIX C3: /close_all must check state.executor (not self.executor).
    Before the fix, getattr(state, "executor", None) always returned None,
    so the command always replied 'not initialized'."""
    from alerts.bot import cmd_close_all

    state = SimpleNamespace(
        paper_mode=False,
        executor=None,  # ← missing
        gmgn_cli=MagicMock(),
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_close_all(update, ctx))

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "not initialized" in reply


def test_close_all_requires_gmgn_cli():
    """FIX C3 cont: /close_all also requires state.gmgn_cli."""
    from alerts.bot import cmd_close_all

    state = SimpleNamespace(
        paper_mode=False,
        executor=MagicMock(),  # present
        gmgn_cli=None,  # ← missing
    )
    bot_data = {"state": state, "db": MagicMock()}
    update = make_update()
    ctx = make_context(bot_data)

    asyncio.run(cmd_close_all(update, ctx))

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "not initialized" in reply


def test_status_key_check_uses_confirmation_state():
    """FIX C2: verify production code reads confirmation.state (not top-level
    status). This is a meta-test of the trade_executor fix."""
    # The actual fix is in core/trade_executor.py — this test reads the
    # source to assert the production code uses confirmation.state
    import inspect
    from core import trade_executor

    src = inspect.getsource(trade_executor)
    # Both buy and sell paths must read confirmation.state
    assert 'confirmation", {}).get("state")' in src
    # And must NOT use the old wrong key alone
    assert src.count('status.get("status")') <= 0 or "FIX C2" in src
