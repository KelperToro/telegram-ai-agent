"""Opt-in live Telegram text and file delivery through the bundled MCP server."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from telegram_bot.core.config import get_settings


def _write_test_png(path: Path, color: tuple[int, int, int]) -> None:
    """Make a small valid image without adding an image-library dependency."""
    width = height = 32
    row = b"\x00" + bytes(color) * width
    pixels = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


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
        image_a = Path(raw_tmp) / "windows-mcp-red.png"
        image_b = Path(raw_tmp) / "windows-mcp-blue.png"
        _write_test_png(image_a, (220, 40, 40))
        _write_test_png(image_b, (40, 80, 220))
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
            media = "--media" in sys.argv[1:]
            calls = (
                (
                    (
                        "send_image",
                        {"file_path": str(image_a), "caption": "Проверка фото через MCP"},
                        "Отправлено фото",
                    ),
                    (
                        "send_image_gallery",
                        {
                            "file_paths": [str(image_a), str(image_b)],
                            "caption": "Проверка галереи через MCP",
                        },
                        "Отправлена галерея",
                    ),
                )
                if media
                else (
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
                )
            )
            for name, arguments, success_prefix in calls:
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
