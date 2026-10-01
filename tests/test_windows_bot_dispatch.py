"""Exercise Telegram update routing through the bot's Windows subprocess path."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.methods import SendMessage
from aiogram.types import (
    Chat,
    Document,
    Message,
    MessageOriginHiddenUser,
    RichBlockParagraph,
    RichMessage,
    Update,
    User,
)

import telegram_bot.__main__ as entrypoint
from telegram_bot.__main__ import process_queue_item
from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.forward import router as forward_router
from telegram_bot.core.handlers.photo import router as photo_router
from telegram_bot.core.handlers.text import router as text_router
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig


class FakeTelegramBot(Bot):
    def __init__(self) -> None:
        super().__init__(token="123:local-test")
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Message:
        assert isinstance(method, SendMessage), type(method)
        chat_id = int(method.chat_id)
        self.sent.append((chat_id, method.text))
        return Message(
            message_id=100 + len(self.sent),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=ChatType.PRIVATE),
            text=method.text,
        )

    async def download(self, file: Any, destination: Any = None, **_kwargs: Any) -> Path:
        assert file == "document"
        path = Path(destination)
        path.write_bytes(b"telegram file content")
        return path


class ImmediateBatcher:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def add_text(self, _key: Any, text: str, message: Message, callback: Any) -> None:
        self.tasks.append(asyncio.create_task(callback(text, message)))

    def add_media(self, _key: Any, message: Message, callback: Any) -> None:
        self.tasks.append(asyncio.create_task(callback([message])))

    def add(self, _key: Any, message: Message, callback: Any) -> None:
        self.tasks.append(asyncio.create_task(callback([message])))

    def get_comment(self, _key: Any) -> list[str]:
        return []

    def get_text_reply_to_message(self, _key: Any) -> None:
        return None

    def get_last_message(self, _key: Any) -> None:
        return None


@pytest.mark.skipif(os.name != "nt", reason="Windows Telegram dispatch")
async def test_text_document_forward_and_rich_updates_reach_codex(tmp_path: Path) -> None:
    bot = FakeTelegramBot()
    settings = Settings(
        telegram_bot_token="123:local-test",
        project_root=str(tmp_path),
        file_cache_dir=str(tmp_path / "data"),
    )
    (tmp_path / "data").mkdir()
    topic_config = TopicConfig(str(tmp_path / "topics.json"), str(tmp_path))
    session_manager = SessionManager(settings, topic_config=topic_config)
    tmux_manager = TmuxManager(tmp_path / "tmux")
    batcher = ImmediateBatcher()
    prompts: list[str] = []

    async def fake_codex(channel_key: Any, prompt: str, _on_event: Any, **_kwargs: Any) -> str:
        prompts.append(prompt)
        session_manager._get_session(
            channel_key
        ).session_id = "018f0000-0000-7000-8000-000000000001"
        return "PONG"

    session_manager.send_stream = fake_codex  # type: ignore[method-assign]

    async def process(
        channel_key: Any,
        prompt: str,
        source_messages: list[Message],
        target_session_id: str | None,
    ) -> None:
        await process_queue_item(
            channel_key,
            prompt,
            source_messages,
            target_session_id,
            bot=bot,
            session_manager=session_manager,
            tmux_manager=tmux_manager,
        )

    queue = MessageQueue(bot, session_manager, process)
    dp = Dispatcher()
    dp.message.outer_middleware(AuthMiddleware([123]))
    dp.message.filter(F.chat.type == ChatType.PRIVATE)
    dp.include_router(forward_router)
    dp.include_router(photo_router)
    dp.include_router(text_router)
    dp["session_manager"] = session_manager
    dp["forward_batcher"] = batcher
    dp["transcriber"] = object()
    dp["message_queue"] = queue
    dp["tmux_manager"] = tmux_manager
    dp["topic_config"] = topic_config

    chat = Chat(id=456, type=ChatType.PRIVATE)
    user = User(id=123, is_bot=False, first_name="Tester")
    now = datetime.now(UTC)
    updates = [
        Update(
            update_id=1,
            message=Message(message_id=1, date=now, chat=chat, from_user=user, text="ping"),
        ),
        Update(
            update_id=2,
            message=Message(
                message_id=2,
                date=now,
                chat=chat,
                from_user=user,
                caption="read this",
                document=Document(
                    file_id="document",
                    file_unique_id="doc1",
                    file_name="пример файл.txt",
                    mime_type="text/plain",
                    file_size=21,
                ),
            ),
        ),
        Update(
            update_id=3,
            message=Message(
                message_id=3,
                date=now,
                chat=chat,
                from_user=user,
                text="forwarded hello",
                forward_origin=MessageOriginHiddenUser(
                    type="hidden_user", date=now, sender_user_name="Alice"
                ),
            ),
        ),
        Update(
            update_id=4,
            message=Message(
                message_id=4,
                date=now,
                chat=chat,
                from_user=user,
                rich_message=RichMessage(
                    blocks=[RichBlockParagraph(type="paragraph", text="rich hello")]
                ),
            ),
        ),
    ]
    try:
        for update in updates:
            await dp.feed_update(bot, update)
            await asyncio.wait_for(asyncio.gather(*batcher.tasks), timeout=5)
            await asyncio.wait_for(asyncio.gather(*queue._background_tasks), timeout=5)
            batcher.tasks.clear()
        assert prompts[0] == "ping"
        assert "read this" in prompts[1]
        assert "пример файл.txt" in prompts[1]
        assert "forwarded hello" in prompts[2]
        assert "rich hello" in prompts[3]
        assert next((tmp_path / "data").glob("*.txt")).read_text() == "telegram file content"
        assert [text for _chat, text in bot.sent if text == "PONG"] == ["PONG"] * 4
        assert all(chat_id == 456 for chat_id, _text in bot.sent)
    finally:
        await queue.shutdown()
        await bot.session.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows bot startup")
async def test_windows_bot_startup_uses_polling_and_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        telegram_bot_token="123:local-test",
        allowed_user_ids=[123],
        project_root=str(tmp_path),
        default_cwd=str(tmp_path),
        file_cache_dir="./data",
        codex_auto_update_enabled=False,
    )
    monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
    # The update-routing test has already attached these module-level routers.
    monkeypatch.setattr(entrypoint, "forward_router", Router(name="startup-forward"))
    monkeypatch.setattr(entrypoint, "photo_router", Router(name="startup-photo"))
    monkeypatch.setattr(entrypoint, "text_router", Router(name="startup-text"))
    broker_dirs: list[Path] = []
    monkeypatch.setattr(entrypoint, "configure_broker", broker_dirs.append)

    async def fake_setup_commands(_bot: Bot) -> None:
        return None

    monkeypatch.setattr(entrypoint, "setup_bot_commands", fake_setup_commands)
    called = False

    async def fake_polling(self: Dispatcher, bot: Bot, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        assert self["session_manager"] is not None
        assert self["message_queue"] is not None
        assert self["tmux_manager"] is not None
        await self.emit_shutdown()
        await bot.session.close()

    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    await entrypoint._start()
    assert called
    assert broker_dirs == [tmp_path / "tmux_sessions"]
