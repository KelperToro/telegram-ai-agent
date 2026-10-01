"""Exercise the Windows MCP launcher and the bot's file delivery contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from telegram_bot.core.handlers.photo import _download_and_format_media, _format_media_prompt
from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config


def _load_bot_server() -> Any:
    path = Path(__file__).resolve().parents[1] / "mcp-servers" / "bot" / "server.py"
    spec = importlib.util.spec_from_file_location("bot_mcp_test_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "nt", reason="Windows MCP launcher")
async def test_windows_codex_mcp_launcher_exposes_file_tools(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:fake\n", encoding="utf-8")
    runtime = tmp_path / "runtime" / "mcp.runtime.json"
    ensure_bot_runtime_mcp_config(
        base_mcp_config=None,
        channel_key=(456, None),
        runtime_path=runtime,
        project_root=root,
    )
    config = json.loads(runtime.read_text(encoding="utf-8"))
    config["mcpServers"]["bot"]["env"]["ENV_FILE"] = str(env_file)
    raw = json.dumps(config).encode("utf-8")
    runtime.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    runner_args = ["-m", "telegram_bot.core.services.codex_mcp", str(runtime), "bot", digest]
    check = subprocess.run(
        [sys.executable, *runner_args], input="", capture_output=True, text=True, timeout=10
    )
    assert check.returncode == 0, check.stderr
    params = StdioServerParameters(
        command=sys.executable,
        args=runner_args,
        env={**os.environ, "BOT_TOKEN": ""},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"send_message", "send_document", "send_image", "send_image_gallery"} <= names
        missing = await session.call_tool(
            "send_document", {"file_path": str(tmp_path / "none.txt")}
        )
        assert "файл не найден" in str(missing.content)


@pytest.mark.skipif(os.name != "nt", reason="Windows MCP launcher")
async def test_windows_mcp_delivers_text_and_files_to_telegram_transport(tmp_path: Path) -> None:
    requests: list[tuple[str, bytes]] = []

    class FakeTelegram(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"result":{}}')

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = Path(__file__).resolve().parents[1] / "mcp-servers" / "bot" / "server.py"
        wrapper = tmp_path / "server_wrapper.py"
        wrapper.write_text(
            "import importlib.util, os\n"
            f"spec = importlib.util.spec_from_file_location('test_bot', {str(source)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module._TELEGRAM_API = os.environ['TEST_TELEGRAM_API']\n"
            "module.mcp.run()\n",
            encoding="utf-8",
        )
        runtime = tmp_path / "mcp.json"
        raw = json.dumps(
            {
                "mcpServers": {
                    "bot": {
                        "command": sys.executable,
                        "args": [str(wrapper)],
                        "env": {
                            "BOT_TOKEN": "123:local-test",
                            "TELEGRAM_CHAT_ID": "456",
                            "TELEGRAM_THREAD_ID": "789",
                            "TELEGRAM_CONTEXT_LOCK": "1",
                            "TEST_TELEGRAM_API": f"http://127.0.0.1:{server.server_port}",
                        },
                    }
                }
            }
        ).encode("utf-8")
        runtime.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        document = tmp_path / "пример файл.txt"
        document.write_text("document body", encoding="utf-8")
        image = tmp_path / "picture.png"
        image.write_bytes(b"image body")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telegram_bot.core.services.codex_mcp", str(runtime), "bot", digest],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            calls = [
                ("send_message", {"text": "Hello"}),
                ("send_document", {"file_path": str(document)}),
                ("send_image", {"file_path": str(image)}),
                ("send_image_gallery", {"file_paths": [str(image), str(image)]}),
            ]
            for name, args in calls:
                result = await session.call_tool(name, args)
                assert not result.isError, result.content
        assert [path.rsplit("/", 1)[-1] for path, _ in requests] == [
            "sendMessage",
            "sendDocument",
            "sendPhoto",
            "sendMediaGroup",
        ]
        for _path, body in requests:
            assert b"456" in body and b"789" in body
        assert b"document body" in requests[1][1]
        assert b"image body" in requests[2][1]
        assert requests[3][1].count(b"image body") == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_bot_mcp_uploads_windows_file_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _load_bot_server()
    document = tmp_path / "пример файл.txt"
    document.write_text("document body", encoding="utf-8")
    photo = tmp_path / "picture.png"
    photo.write_bytes(b"fake-image-bytes")
    calls: list[tuple[str, bytes]] = []

    def fake_post(
        _client: httpx.Client,
        url: str,
        data: dict[str, str],
        files: dict[str, tuple[str, Any]] | None = None,
    ) -> httpx.Response:
        assert data["chat_id"] == "456"
        assert data["message_thread_id"] == "789"
        assert files is not None
        field, (filename, handle) = next(iter(files.items()))
        calls.append((f"{url.rsplit('/', 1)[-1]}:{field}:{filename}", handle.read()))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(server, "_post_with_flood_retry", fake_post)
    assert server._send_document("123:fake", 456, document, "caption", 789).startswith("Отправлен")
    assert server._send_photo("123:fake", 456, photo, "caption", 789).startswith("Отправлено фото")
    assert calls == [
        (f"sendDocument:document:{document.name}", b"document body"),
        (f"sendPhoto:photo:{photo.name}", b"fake-image-bytes"),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows media paths")
async def test_telegram_media_downloads_become_codex_file_prompts(tmp_path: Path) -> None:
    class FakeBot:
        async def download(self, file_id: str, *, destination: Path) -> None:
            destination.write_bytes({"document": b"report", "photo": b"image"}[file_id])

    document = SimpleNamespace(
        photo=None,
        document=SimpleNamespace(
            file_id="document",
            file_unique_id="doc1",
            file_name="отчёт файл.txt",
            mime_type="text/plain",
        ),
        caption="Прочитай файл",
    )
    photo = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo", file_unique_id="pic1")],
        document=None,
        caption="Что на фото?",
    )
    bot = FakeBot()
    items = [
        await _download_and_format_media(document, bot, tmp_path),
        await _download_and_format_media(photo, bot, tmp_path),
    ]
    assert all(item is not None and item["path"] for item in items)
    assert [Path(item["path"]).read_bytes() for item in items if item is not None] == [
        b"report",
        b"image",
    ]
    prompt = _format_media_prompt([item for item in items if item is not None])
    assert "Прочитай файл" in prompt
    assert "Что на фото?" in prompt
    assert all(item is not None and item["path"] in prompt for item in items)
