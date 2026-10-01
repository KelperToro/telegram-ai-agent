"""Shared Codex session and file-lock behavior used by native Windows runs."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_bot.core.config import Settings
from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config
from telegram_bot.core.services.claude import SessionManager
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
