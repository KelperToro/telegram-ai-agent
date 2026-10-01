"""Shared Codex session and file-lock behavior used by native Windows runs."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.config import Settings
from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config
from telegram_bot.core.services.claude import SessionData, SessionManager
from telegram_bot.core.services.codex_app_server import (
    CodexAppServerError,
    _thread_list_label,
    list_codex_thread_titles,
    stream_event_from_notification,
)
from telegram_bot.core.services.codex_mcp import build_codex_mcp_config_args
from telegram_bot.core.services.process_cleanup import (
    processes_by_sid,
    tagged_processes,
    terminate_processes,
)
from telegram_bot.core.services.resume_listing import list_sessions
from telegram_bot.core.utils.file_lock import FileLock


def test_resume_lists_desktop_session_for_matching_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / ".codex" / "sessions" / "2026" / "09" / "desktop.jsonl"
    transcript.parent.mkdir(parents=True)
    session_id = "018f0000-0000-7000-8000-000000000001"
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": str(cwd),
                        "originator": "Codex Desktop",
                        "source": "vscode",
                    },
                },
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}},
            )
        ),
        encoding="utf-8",
    )

    entries = list_sessions(cwd, home=tmp_path)

    assert [(item.provider, item.session_id, item.preview) for item in entries] == [
        ("codex", session_id, "Hello")
    ]


def test_resume_excludes_subagent_with_second_desktop_metadata(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = tmp_path / ".codex" / "sessions" / "2026" / "10" / "01"
    root.mkdir(parents=True)
    session_id = "018f0000-0000-7000-8000-000000000001"
    (root / "subagent.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": str(cwd),
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    },
                },
                {
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": str(cwd), "source": "vscode"},
                },
            )
        ),
        encoding="utf-8",
    )

    assert list_sessions(cwd, home=tmp_path, all_codex=True) == []


@pytest.mark.asyncio
async def test_resume_reads_name_of_chat_missing_from_thread_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import telegram_bot.core.services.codex_app_server as app_server

    sid = "018f0000-0000-7000-8000-000000000001"

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "thread/list":
                assert params["archived"] in (False, True)
                return {"data": []}
            assert method == "thread/read"
            assert params == {"threadId": sid, "includeTurns": False}
            return {"thread": {"id": sid, "name": "Older desktop chat"}}

    monkeypatch.setattr(app_server, "CodexAppServerClient", FakeClient)
    assert await list_codex_thread_titles(fallback_ids=[sid]) == {sid: "Older desktop chat"}


def test_desktop_first_turn_progress_preserves_status_and_commentary() -> None:
    started = stream_event_from_notification(
        {"method": "turn/started", "params": {"turn": {"id": "turn-1"}}}
    )
    command = stream_event_from_notification(
        {
            "method": "item/started",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "command": "git status"},
            },
        }
    )
    commentary = stream_event_from_notification(
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "phase": "commentary", "text": "Checking files"},
            },
        }
    )
    final = stream_event_from_notification(
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "Done"},
            },
        }
    )
    completed = stream_event_from_notification(
        {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
    )

    assert started is not None and (started.type, started.turn_id) == ("turn_start", "turn-1")
    assert command is not None and command.type == "status" and "Git" in command.content
    assert commentary is not None and (commentary.type, commentary.content) == (
        "text",
        "Checking files",
    )
    assert final is None
    assert completed is not None and (completed.type, completed.turn_id) == ("turn_end", "turn-1")


def test_codex_exec_attaches_photo_on_new_and_resumed_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.services.claude as claude

    monkeypatch.setattr(claude.CODEX_ADAPTER, "binary", lambda: "codex")
    monkeypatch.setattr(claude, "codex_process_env", lambda: {})
    monkeypatch.setattr(claude, "discover_codex_mcp_server_names", lambda *_a, **_kw: [])
    monkeypatch.setattr(claude, "build_codex_mcp_config_args", lambda *_a, **_kw: [])
    manager = SessionManager(
        Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    )
    session = SessionData(engine="codex", cwd=str(tmp_path), chat_id=123, thread_id=None)
    photo = str(tmp_path / "photo.jpg")

    fresh = manager._build_exec_command("Describe it", session, image_paths=(photo,))
    assert fresh.argv[-3:] == ["--image", photo, "-"]
    session.session_id = "018f0000-0000-7000-8000-000000000001"
    resumed = manager._build_exec_command("Describe it", session, image_paths=(photo,))
    assert resumed.argv[-3:] == ["--image", photo, "-"]


@pytest.mark.asyncio
async def test_desktop_first_turn_survives_unsupported_thread_naming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.services.claude as claude

    calls: list[str] = []
    turn_inputs: list[dict[str, object]] = []

    class FakeClient:
        process = None

        def __init__(self, *, cwd: str) -> None:
            assert cwd == str(tmp_path)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def call(self, method: str, _params: dict[str, object]) -> dict[str, object]:
            calls.append(method)
            if method == "thread/start":
                return {"thread": {"id": "018f0000-0000-7000-8000-000000000001"}}
            if method == "thread/name/set":
                raise CodexAppServerError("unsupported method")
            if method == "turn/start":
                turn_inputs.extend(_params["input"])
                return {"turn": {"id": "turn-1"}}
            raise AssertionError(method)

        async def wait_for_turn(
            self,
            _thread_id: str,
            *,
            timeout: float,
            on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
        ) -> None:
            assert timeout > 0
            result = on_notification(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Done",
                        }
                    },
                }
            )
            if result is not None:
                await result

    monkeypatch.setattr(claude, "CodexAppServerClient", FakeClient)
    monkeypatch.setattr(claude, "codex_app_config", lambda *_args: {})
    manager = SessionManager(
        Settings(telegram_bot_token="123:test", project_root=str(tmp_path), _env_file=None)
    )
    session = SessionData(engine="codex", cwd=str(tmp_path), chat_id=123, thread_id=None)
    answer = await manager._run_codex_app_first_turn(
        "Hello", session, lambda _event: None, mcp_config="", image_paths=("image.jpg",)
    )

    assert answer == "Done"
    assert session.session_id == "018f0000-0000-7000-8000-000000000001"
    assert calls == ["thread/start", "thread/name/set", "turn/start"]
    assert turn_inputs[-1] == {"type": "localImage", "path": "image.jpg"}


def test_resume_uses_desktop_preview_when_thread_has_no_name() -> None:
    assert _thread_list_label({"name": "  Named   discussion ", "preview": "ignored"}) == (
        "Named discussion"
    )
    assert _thread_list_label({"preview": "  Fix   image upload on Windows  "}) == (
        "Fix image upload on Windows"
    )
    assert _thread_list_label({"preview": "<command-message>internal"}) is None


def test_file_lock_rejects_second_nonblocking_holder(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    with FileLock(target), pytest.raises(BlockingIOError), FileLock(target, blocking=False):
        pytest.fail("Second lock was acquired")


def test_bundled_bot_mcp_launches_without_bash_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "mcp.runtime.json"
    ensure_bot_runtime_mcp_config(
        base_mcp_config=None,
        channel_key=(123, None),
        runtime_path=path,
        project_root=tmp_path,
    )
    bot = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["bot"]

    assert bot["command"] == (sys.executable if os.name == "nt" else "bash")
    assert bot["args"][0].endswith("start.py" if os.name == "nt" else "start.sh")
    assert "TELEGRAM_BOT_TOKEN" not in bot["env"]
    assert build_codex_mcp_config_args(str(path))


def test_python_bot_mcp_launcher_reads_token_and_exits_on_eof(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:test\n", encoding="utf-8")
    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, APP_ROOT=str(project_root), ENV_FILE=str(env_file))
    env.pop("BOT_TOKEN", None)

    result = subprocess.run(
        [sys.executable, str(project_root / "mcp-servers" / "bot" / "start.py")],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree")
async def test_cancel_kills_codex_wrapper_and_child(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if child_pid_file.exists():
                break
            await asyncio.sleep(0.1)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        manager = SessionManager(
            Settings(telegram_bot_token="123:test", project_root=str(tmp_path))
        )
        await manager._kill_process(process)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, child_pid)
        if handle:
            exit_code = ctypes.c_ulong()
            try:
                assert kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                assert exit_code.value != 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if child_pid_file.exists():
            subprocess.run(
                ["taskkill", "/PID", child_pid_file.read_text(), "/T", "/F"],
                capture_output=True,
                check=False,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows process diagnostics")
def test_windows_runtime_diagnostics_find_and_stop_tagged_process(tmp_path: Path) -> None:
    runtime = str(tmp_path / "mcp.runtime.json")
    env = dict(
        os.environ,
        AI_ASSISTANT_CHANNEL_KEY="456:789",
        AI_ASSISTANT_MCP_RUNTIME=runtime,
    )
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
    try:
        matches = tagged_processes(channel_key=(456, 789), runtime_path=runtime)
        assert process.pid in {item.pid for item in matches}
        assert process.pid in {item.pid for item in processes_by_sid(process.pid)}
        terminate_processes(matches)
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
