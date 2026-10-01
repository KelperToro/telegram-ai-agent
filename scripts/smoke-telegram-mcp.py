"""Opt-in live Telegram text and file delivery through the bundled MCP server."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from telegram_bot.core.config import get_settings


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    settings = get_settings()
    if not settings.allowed_user_ids:
        print("ALLOWED_USER_IDS is empty", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="telegram-mcp-smoke-") as raw_tmp:
        document = Path(raw_tmp) / "windows-mcp-test.txt"
        document.write_text(
            "This file was sent by the local Windows Telegram MCP server.\n",
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(root / "mcp-servers" / "bot" / "start.py")],
            env={
                **os.environ,
                "APP_ROOT": str(root),
                "ENV_FILE": str(root / ".env"),
                "TELEGRAM_CHAT_ID": str(settings.allowed_user_ids[0]),
                "TELEGRAM_CONTEXT_LOCK": "1",
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments, success_prefix in (
                (
                    "send_message",
                    {"text": "Проверка отправки текста через локальный MCP."},
                    "Отправлено сообщение",
                ),
                (
                    "send_document",
                    {"file_path": str(document), "caption": "Проверка файла через MCP"},
                    "Отправлен:",
                ),
            ):
                result = await session.call_tool(name, arguments)
                returned = " ".join(
                    part.text for part in result.content if isinstance(part, TextContent)
                )
                if result.isError or not returned.startswith(success_prefix):
                    print(f"{name} failed: {result.content}", file=sys.stderr)
                    return 1
                print(f"{name}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
