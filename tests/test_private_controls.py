"""Private chat controls share forum command behavior without shared settings."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.commands import (
    _pin_current_session,
    _resume_caption,
    handle_engine_command,
    handle_goal,
    handle_mode_command,
    handle_stream_mode,
    on_engine_click,
    on_exec_mode_click,
    on_resume_pick,
    on_stream_mode_click,
)
from telegram_bot.core.keyboards import resume_keyboard
from telegram_bot.core.services.claude import CCSessionBusyError, SessionManager
from telegram_bot.core.services.codex_app_server import CodexAppServerError
from telegram_bot.core.services.picker_store import PickerState, PickerStore
from telegram_bot.core.services.resume_listing import SessionEntry, list_sessions
from telegram_bot.core.services.topic_config import TopicConfig, config_id_for_channel
from telegram_bot.core.services.topic_runtime import BotDefaults


class FakeMessage:
    def __init__(self, chat_id: int, thread_id: int | None = None) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_thread_id = thread_id
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.text = ""

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.sent.append(text)

    async def edit_text(self, text: str, **_kwargs: Any) -> None:
        self.edited.append(text)


class FakeCallback:
    def __init__(self, message: FakeMessage, data: str) -> None:
        self.message = message
        self.data = data
        self.from_user = SimpleNamespace(id=123)
        self.answered: list[str] = []

    async def answer(self, text: str = "", **_kwargs: Any) -> None:
        self.answered.append(text)


class FakeTmux:
    def __init__(self) -> None:
        self.closed: list[tuple[int, int | None]] = []

    def is_processing(self, _key: object) -> bool:
        return False

    def is_active(self, _key: object) -> bool:
        return False

    def get_active_session_id(self, _key: object) -> None:
        return None

    async def close_buffer(self, key: tuple[int, int | None]) -> None:
        self.closed.append(key)


class FakeQueue:
    def is_busy(self, _key: object) -> bool:
        return False


class FakeSessions:
    def __init__(self) -> None:
        self.cleared: list[tuple[int, int | None]] = []

    async def clear_provider_session(self, key: tuple[int, int | None]) -> None:
        self.cleared.append(key)


@pytest.mark.asyncio
async def test_private_chat_can_select_and_persist_codex_tui_and_stream_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "topic_config.json"
    config = TopicConfig(str(path), str(tmp_path))
    message = FakeMessage(chat_id=456)
    tmux = FakeTmux()
    queue = FakeQueue()
    sessions = FakeSessions()

    await handle_mode_command(message, config)  # type: ignore[arg-type]
    await handle_engine_command(message, config)  # type: ignore[arg-type]
    await handle_stream_mode(message, config)  # type: ignore[arg-type]
    assert len(message.sent) == 3

    await on_exec_mode_click(  # type: ignore[arg-type]
        FakeCallback(message, "exec_mode:tmux"), config, tmux, queue
    )
    await on_engine_click(  # type: ignore[arg-type]
        FakeCallback(message, "engine:codex"), config, tmux, queue, sessions
    )
    await on_stream_mode_click(  # type: ignore[arg-type]
        FakeCallback(message, "stream_mode:minimal"), config, tmux
    )

    private_id = config_id_for_channel((456, None))
    assert private_id == -456
    reloaded = TopicConfig(str(path), str(tmp_path))
    saved = reloaded.get_topic(private_id)
    assert (saved.exec_mode, saved.engine, saved.stream_mode) == (
        "tmux",
        "codex",
        "minimal",
    )
    assert reloaded.get_topic(config_id_for_channel((789, None))).engine == "claude"
    assert reloaded.get_topic(config_id_for_channel((-100, None))).engine == "claude"
    assert sessions.cleared == [(456, None)]
    assert tmux.closed == [(456, None)]


def test_forum_settings_id_is_unchanged() -> None:
    assert config_id_for_channel((-100, 42)) == 42
    assert config_id_for_channel((-100, None)) is None


@pytest.mark.asyncio
async def test_private_resume_selects_codex_chat_from_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.handlers.commands as commands

    monkeypatch.setattr(commands, "is_engine_available", lambda _provider: True)
    current = tmp_path / "current"
    old_project = tmp_path / "old-project"
    current.mkdir()
    old_project.mkdir()
    sid = "018f0000-0000-7000-8000-000000000001"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")
    entry = SessionEntry(
        "codex", sid, transcript, "old work", time.time(), 0, old_project, "Old discussion"
    )
    config = TopicConfig(str(tmp_path / "topics.json"), str(current))
    settings = Settings(telegram_bot_token="123:test", project_root=str(current), _env_file=None)
    sessions = SessionManager(settings, topic_config=config)
    picker = PickerStore()
    token = picker.put(PickerState(456, None, current, "claude", (entry,), time.time()))
    callback = FakeCallback(FakeMessage(456), f"rs:s:{token}:0")

    await on_resume_pick(  # type: ignore[arg-type]
        callback,
        sessions,
        config,
        FakeTmux(),
        picker,
        BotDefaults(current, current / "mcp.json"),
    )

    selected = config.get_topic(-456)
    assert selected.engine == "codex"
    assert selected.cwd == str(old_project)
    assert sessions.get_current_session_id((456, None)) == sid
    assert callback.message.edited
    assert "Old discussion" in callback.message.edited[0]
    assert str(old_project) in callback.message.edited[0]


def test_private_resume_lists_all_codex_projects_and_deduplicates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".codex" / "sessions" / "2026" / "10" / "01"
    root.mkdir(parents=True)
    sid = "018f0000-0000-7000-8000-000000000001"
    old_cwd = tmp_path / "old-project"
    old_cwd.mkdir()
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": str(old_cwd)}}
    for idx in range(2):
        path = root / f"rollout-{idx}-{sid}.jsonl"
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

    assert list_sessions(tmp_path, home=home) == []
    all_entries = list_sessions(tmp_path, home=home, all_codex=True)
    assert len(all_entries) == 1
    assert all_entries[0].cwd == old_cwd


def test_resume_picker_shows_chat_name_and_project_on_buttons(tmp_path: Path) -> None:
    sid = "018f0000-0000-7000-8000-000000000001"
    project = tmp_path / "kombain"
    entry = SessionEntry(
        "codex",
        sid,
        tmp_path / "rollout.jsonl",
        "fallback",
        time.time(),
        1_200_000_000,
        project,
        "Fix database sync",
    )
    newer = replace(
        entry,
        session_id="018f0000-0000-7000-8000-000000000002",
        title="Newer chat",
        mtime=entry.mtime + 1,
    )
    ordered = _pin_current_session((newer, entry), sid)
    assert ordered == (entry, newer)
    keyboard = resume_keyboard(ordered, page=0, current_session_id=sid, token="abc")
    button = keyboard.inline_keyboard[0][0]
    assert "kombain" in button.text
    assert "Fix database sync" in button.text
    assert button.text.startswith("✅")
    assert button.callback_data == "rs:s:abc:0"

    caption = _resume_caption(
        tmp_path,
        page=0,
        total_pages=1,
        entries=ordered,
        current_session_id=sid,
        all_projects=True,
    )
    assert "Fix database sync" in caption
    assert caption.index("Fix database sync") < caption.index("Newer chat")
    assert str(project) in caption
    assert "1200" not in caption


@pytest.mark.asyncio
async def test_goal_command_uses_restored_codex_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.handlers.commands as commands

    settings = Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    first = SessionManager(settings)
    sid = "018f0000-0000-7000-8000-000000000001"
    await first.override_session((456, None), sid, provider="codex")
    first.save_mapping()
    restored = SessionManager(settings)
    restored.load_mapping()
    assert restored.get_current_session_id((456, None)) == sid
    message = FakeMessage(456)
    message.text = "/goal"

    async def fake_get_goal(thread_id: str) -> dict[str, object]:
        assert thread_id == sid
        return {"objective": "Finish the task", "status": "active", "tokensUsed": 12}

    monkeypatch.setattr(commands, "get_thread_goal", fake_get_goal)
    await handle_goal(message, restored, FakeTmux())  # type: ignore[arg-type]
    assert "Finish the task" in message.sent[-1]


@pytest.mark.asyncio
async def test_busy_codex_chat_keeps_selected_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.services.claude as claude

    config = TopicConfig(str(tmp_path / "topics.json"), str(tmp_path))
    assert await config.update_engine(-456, "codex")
    settings = Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    manager = SessionManager(settings, topic_config=config)
    sid = "018f0000-0000-7000-8000-000000000001"
    await manager.override_session((456, None), sid, provider="codex")
    monkeypatch.setattr(claude, "choose_available_engine", lambda _requested: "codex")

    async def busy(*_args: Any, **_kwargs: Any) -> str:
        raise CCSessionBusyError(sid)

    monkeypatch.setattr(manager, "_run_cc_stream", busy)
    result = await manager.send_stream((456, None), "hello", lambda _event: None)
    assert "another" in result.lower() or "занят" in result.lower()
    assert manager.get_current_session_id((456, None)) == sid


@pytest.mark.asyncio
async def test_cancelled_desktop_first_turn_keeps_created_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.services.claude as claude

    settings = Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    manager = SessionManager(settings)
    session = manager._get_session((456, None))
    session.engine = "codex"
    sid = "018f0000-0000-7000-8000-000000000001"
    monkeypatch.setattr(claude, "choose_available_engine", lambda _requested: "codex")

    async def interrupted(*_args: Any, **_kwargs: Any) -> str:
        session.session_id = sid
        session.cancelled = True
        raise CodexAppServerError("app-server closed during turn")

    monkeypatch.setattr(manager, "_run_cc_stream", interrupted)
    assert await manager.send_stream((456, None), "hello", lambda _event: None) == ""
    assert manager.get_current_session_id((456, None)) == sid
    assert not session.cancelled
