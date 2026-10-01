"""Create a Desktop-visible Codex chat through the bot's Windows path."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.claude import SessionData, SessionManager


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    telegram_mcp = "--telegram-mcp" in sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="telegram-codex-smoke-") as cache:
        if telegram_mcp:
            settings = Settings()
            if not settings.allowed_user_ids:
                raise SystemExit("ALLOWED_USER_IDS is empty")
            chat_id = settings.allowed_user_ids[0]
            prompt = (
                "Use the bot MCP tool to send exactly DESKTOP_MCP_OK to this Telegram chat. "
                "Then reply exactly BOOTSTRAPMCPDONE."
            )
            expected = "BOOTSTRAPMCPDONE"
        else:
            settings = Settings(
                telegram_bot_token="123:test",
                project_root=str(root),
                file_cache_dir=cache,
                _env_file=None,
            )
            chat_id = 0
            tool_events = "--tool-events" in sys.argv[1:]
            prompt = (
                "Read README.windows.md, then reply exactly BOOTSTRAPTOOLOK"
                if tool_events
                else "Reply exactly BOOTSTRAPOK"
            )
            expected = "BOOTSTRAPTOOLOK" if tool_events else "BOOTSTRAPOK"
        manager = SessionManager(settings)
        session = SessionData(
            engine="codex",
            cwd=str(root),
            chat_id=chat_id,
            thread_id=None,
            mcp_config=manager.default_mcp_config_path(),
        )
        events: list[StreamEvent] = []
        answer = await manager._run_cc_stream(prompt, session, events.append)
        print("thread_id:", session.session_id)
        print("answer:", answer.strip())
        print("progress:", [event.type for event in events])
        if answer.strip() != expected or session.session_id is None:
            raise SystemExit(1)
        if not {"turn_start", "turn_end"} <= {event.type for event in events}:
            raise SystemExit("Missing first-turn progress lifecycle")
        if not telegram_mcp and tool_events and not any(event.type == "status" for event in events):
            raise SystemExit("Missing first-turn tool progress")


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Windows only")
    asyncio.run(main())
