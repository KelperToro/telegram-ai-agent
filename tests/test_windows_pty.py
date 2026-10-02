"""Real ConPTY smoke test for the Windows session broker."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from telegram_bot.core.services.process_cleanup import processes_by_sid
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState
from telegram_bot.core.services.windows_pty import configure_broker, run_tmux


@pytest.mark.skipif(os.name != "nt", reason="ConPTY requires Windows")
def test_windows_broker_persists_and_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # The adapter must use native ConPTY even if the operator's shell has
    # configured pywinpty's legacy backend for another application.
    monkeypatch.setenv("PYWINPTY_BACKEND", "1")
    configure_broker(tmp_path)
    broker_file = Path(os.environ["TELEGRAM_BOT_PTY_BROKER_FILE"])
    try:
        started = run_tmux(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "smoke",
                "-x",
                "80",
                "-y",
                "24",
                sys.executable,
                "-c",
                "import msvcrt,time; print('PTY_READY', flush=True); "
                "print('KEY:'+msvcrt.getwch(), flush=True); time.sleep(20)",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert started.returncode == 0, started.stderr
        broker_pid = json.loads(broker_file.read_text(encoding="utf-8"))["pid"]
        child_names = [item.command.casefold() for item in processes_by_sid(broker_pid)]
        assert "winpty-agent.exe" not in child_names, child_names
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pane = run_tmux(
                ["tmux", "capture-pane", "-t", "=smoke:", "-p"],
                capture_output=True,
                text=True,
            )
            if "PTY_READY" in pane.stdout:
                break
            time.sleep(0.1)
        assert "PTY_READY" in pane.stdout
        assert run_tmux(["tmux", "has-session", "-t", "=smoke:"]).returncode == 0
        second_client = subprocess.run(
            [
                sys.executable,
                "-c",
                "from telegram_bot.core.services.windows_pty import run_tmux; "
                "print(run_tmux(['tmux','has-session','-t','=smoke:']).returncode)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert second_client.stdout.strip() == "0"
        first_manager = TmuxManager(tmp_path)
        first_manager._sessions[(123, None)] = TmuxSessionState(
            session_name="smoke",
            session_dir=str(tmp_path / "smoke"),
            session_id="12345678-1234-1234-1234-123456789abc",
            mode="free",
            cwd=str(tmp_path),
            mcp_config="",
            chat_id=123,
            runner_version="claude-tui-v1",
            provider="claude",
        )
        first_manager.persist_state()
        restarted_manager = TmuxManager(tmp_path)
        assert (123, None) in restarted_manager.restore_all()
        assert (
            run_tmux(["tmux", "load-buffer", "-b", "test", "-"], input="A", text=True).returncode
            == 0
        )
        assert (
            run_tmux(["tmux", "paste-buffer", "-p", "-b", "test", "-t", "=smoke:"]).returncode == 0
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pane = run_tmux(
                ["tmux", "capture-pane", "-t", "=smoke:", "-p"],
                capture_output=True,
                text=True,
            )
            if "KEY:A" in pane.stdout:
                break
            time.sleep(0.1)
        assert "KEY:A" in pane.stdout
        assert run_tmux(["tmux", "kill-session", "-t", "=smoke:"]).returncode == 0
    finally:
        if broker_file.exists():
            info = json.loads(broker_file.read_text(encoding="utf-8"))
            os.kill(info["pid"], signal.SIGTERM)
