"""Opening a selected chat as a TUI preserves its context and respects busy work."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers import tail
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults


class TelegramBot(Bot):
    def __init__(self) -> None:
        super().__init__(token="123:test")
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Message:
        assert isinstance(method, SendMessage)
        self.sent.append((method.text, {"reply_markup": method.reply_markup}))
        return Message(message_id=43, date=datetime.now(UTC), chat=Chat(id=123, type="private"))


async def dispatch(
    command: Any, manager: Any, sessions: Any, config: Any, queue: Any, defaults: BotDefaults
) -> TelegramBot:
    bot = TelegramBot()
    router = Router()
    router.message.register(command)
    dp = Dispatcher()
    dp.include_router(router)
    message = Message(
        message_id=42, date=datetime.now(UTC), chat=Chat(id=123, type="private"), text="/tui"
    )
    try:
        await dp.feed_update(
            bot,
            Update(update_id=1, message=message),
            tmux_manager=manager,
            session_manager=sessions,
            topic_config=config,
            message_queue=queue,
            bot_defaults=defaults,
        )
    finally:
        await bot.session.close()
    return bot


class Queue:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy

    def is_busy(self, _key: object) -> bool:
        return self.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [tail.handle_tail_command, tail.handle_tui_button])
@pytest.mark.parametrize("failure", [False, True])
async def test_tui_opens_selected_subprocess_chat_or_rolls_back_on_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: Any,
    failure: bool,
) -> None:
    sid = "018f0000-0000-7000-8000-000000000001"
    codex_home = tmp_path / "codex"
    transcripts = codex_home / "sessions"
    transcripts.mkdir(parents=True)
    (transcripts / "chat.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": sid, "cwd": str(tmp_path)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TELEGRAM_CODEX_SHARED_HOME", "1")
    config = TopicConfig(str(tmp_path / "topics.json"), str(tmp_path))
    await config.update_engine(-123, "codex")
    settings = Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    sessions = SessionManager(settings, topic_config=config)
    await sessions.override_session((123, None), sid, provider="codex")
    manager = TmuxManager(tmp_path / "tmux")
    alive = False

    async def spawn(**_kwargs: Any) -> None:
        nonlocal alive
        if failure:
            raise RuntimeError("test terminal spawn failed")
        alive = True

    monkeypatch.setattr(manager, "_spawn_tmux", spawn)
    monkeypatch.setattr(manager, "_tmux_alive", lambda _name: alive)
    monkeypatch.setattr(
        tail, "run_tmux", lambda *a, **k: subprocess.CompletedProcess(a, 0, "Codex ready", "")
    )
    bot = await dispatch(
        command,
        manager,
        sessions,
        config,
        Queue(),
        BotDefaults(
            cwd=tmp_path,
            mcp_config=tmp_path / "mcp.json",
        ),
    )

    assert sessions.get_current_session_id((123, None)) == sid
    assert manager.is_active((123, None)) is not failure
    assert config.get_topic(-123).exec_mode == ("subprocess" if failure else "tmux")
    if not failure:
        assert manager.get_session_id((123, None)) == sid
        assert bot.sent[-1][0] == "<pre>Codex ready</pre>"
        assert bot.sent[-1][1]["reply_markup"].inline_keyboard
    else:
        assert bot.sent and "<pre>" not in bot.sent[-1][0]


@pytest.mark.asyncio
async def test_tui_does_not_change_mode_while_a_request_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "018f0000-0000-7000-8000-000000000001"
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "chat.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": sid, "cwd": str(tmp_path)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("TELEGRAM_CODEX_SHARED_HOME", "1")
    config = TopicConfig(str(tmp_path / "topics.json"), str(tmp_path))
    await config.update_engine(-123, "codex")
    settings = Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    sessions = SessionManager(settings, topic_config=config)
    await sessions.override_session((123, None), sid, provider="codex")
    manager = TmuxManager(tmp_path / "tmux")

    async def failed_spawn(**_kwargs: Any) -> None:
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(manager, "_spawn_tmux", failed_spawn)
    bot = await dispatch(
        tail.handle_tail_command,
        manager,
        sessions,
        config,
        Queue(True),
        BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json"),
    )  # type: ignore[arg-type]

    assert not manager.is_active((123, None))
    assert config.get_topic(-123).exec_mode == "subprocess"
    assert sessions.get_current_session_id((123, None)) == sid
    assert len(bot.sent) == 1
