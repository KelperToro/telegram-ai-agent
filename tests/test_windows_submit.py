"""Windows Codex submission can be acknowledged after its initial UI redraw."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from telegram_bot.core.services import tmux_manager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex acknowledgement budget")
async def test_windows_submit_waits_for_slow_codex_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TmuxManager(tmp_path)
    key = (123, None)
    state = TmuxSessionState(
        session_name="slow-codex",
        session_dir=str(tmp_path),
        session_id=None,
        mode="free",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=123,
        runner_version="codex-tui-v1",
        provider="codex",
    )
    prompt = "Verify delayed submission"
    pasted = False
    first_enter_at: float | None = None

    async def paste(name: str, text: str, *, submit_enter: bool) -> None:
        nonlocal pasted
        assert name == "slow-codex" and text == prompt and not submit_enter
        pasted = True

    async def enter(name: str) -> None:
        nonlocal first_enter_at
        assert name == "slow-codex"
        if first_enter_at is None:
            first_enter_at = time.monotonic()

    async def capture(name: str) -> str:
        assert name == "slow-codex"
        acknowledged = first_enter_at is not None and time.monotonic() - first_enter_at > 2.2
        value = prompt if pasted and not acknowledged else "Ask Codex to do anything"
        return f"\u203a {value}\n\n  GPT-6.1-Sol · Context 100% left\n"

    # Only the external terminal I/O is substituted. The real send policy,
    # observation timing, input parsing and success decision stay in use.
    monkeypatch.setattr(tmux_manager, "send_text_to_tmux", paste)
    monkeypatch.setattr(tmux_manager, "send_enter", enter)
    monkeypatch.setattr(tmux_manager, "capture_pane", capture)

    assert await manager._safe_send_codex(key, state, prompt)
