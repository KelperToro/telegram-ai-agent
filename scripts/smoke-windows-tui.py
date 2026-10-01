"""Opt-in live smoke test for Codex TUI through the Windows ConPTY broker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.windows_pty import configure_broker, run_tmux


async def main() -> int:
    if os.name != "nt":
        print("This smoke test requires Windows.", file=sys.stderr)
        return 2

    app_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="telegram-codex-tui-") as raw_tmp:
        tmp = Path(raw_tmp)
        # Use the already trusted checkout: a fresh temp folder triggers
        # Codex's folder trust dialog and intentionally blocks TUI input.
        project = app_root
        env_file = tmp / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=123:local-test\n", encoding="utf-8")
        base_mcp = tmp / "mcp.json"
        base_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "bot": {
                            "command": sys.executable,
                            "args": [str(app_root / "mcp-servers" / "bot" / "start.py")],
                            "env": {"APP_ROOT": str(app_root), "ENV_FILE": str(env_file)},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        settings = Settings(
            telegram_bot_token="123:local-test",
            project_root=str(app_root),
            agent_workspace_root=str(tmp),
            default_cwd=str(project),
            file_cache_dir=str(tmp / "data"),
        )
        sessions = SessionManager(settings)
        tmux_dir = tmp / "tmux"
        configure_broker(tmux_dir)
        broker_file = Path(os.environ["TELEGRAM_BOT_PTY_BROKER_FILE"])
        manager = TmuxManager(tmux_dir)
        key = (123, None)
        events: list[tuple[str, str]] = []
        answer_seen = asyncio.Event()
        stream_task: asyncio.Task[str] | None = None

        def on_event(event: StreamEvent) -> None:
            kind = event.type
            content = event.content
            events.append((kind, content))
            if kind == "result_message" and "WINDOWSTUIOK" in content:
                answer_seen.set()

        try:
            started = await asyncio.wait_for(
                manager.start_session(
                    key,
                    mode="free",
                    cwd=str(project),
                    mcp_config=str(base_mcp),
                    chat_id=123,
                    session_manager=sessions,
                    provider="codex",
                ),
                timeout=60,
            )
            if not started or not manager.is_active(key):
                print("Codex TUI did not start", file=sys.stderr)
                return 1
            stream_task = asyncio.create_task(
                manager.send_stream(key, "Reply with exactly WINDOWSTUIOK", on_event)
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(answer_seen.wait(), timeout=120)
            print(f"Events: {events[-8:]!r}")
            if not answer_seen.is_set():
                state = manager._sessions.get(key)
                if state is not None:
                    pane = run_tmux(
                        [
                            "tmux",
                            "capture-pane",
                            "-t",
                            f"={state.session_name}:",
                            "-p",
                            "-S",
                            "-35",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    print(f"TUI pane: {pane.stdout!r}")
                print("No Codex answer reached the TUI transcript tail", file=sys.stderr)
                return 1
            return 0
        finally:
            if stream_task is not None:
                await manager.cancel(key)
                await asyncio.wait_for(stream_task, timeout=10)
            await manager.kill(key)
            if broker_file.exists():
                info = json.loads(broker_file.read_text(encoding="utf-8"))
                os.kill(info["pid"], signal.SIGTERM)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
