"""Opt-in live Windows smoke test for the bot's Codex subprocess conversation."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager


async def main() -> int:
    if os.name != "nt":
        print("This smoke test requires Windows.", file=sys.stderr)
        return 2

    app_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="telegram-codex-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        cwd = tmp / "project"
        cwd.mkdir()
        env_file = tmp / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=123:local-test\n", encoding="utf-8")
        mcp_config = tmp / "mcp.json"
        mcp_config.write_text(
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
            default_cwd=str(cwd),
            file_cache_dir=str(tmp / "data"),
            cc_query_timeout_sec=180,
            cc_inactivity_kill_sec=180,
        )
        manager = SessionManager(settings)
        key = (123, None)
        session = manager._get_session(key)
        session.engine = "codex"
        session.cwd = str(cwd)
        session.mcp_config = str(mcp_config)

        async def on_event(_event: object) -> None:
            return None

        first = await manager.send_stream(
            key, "Remember the secret word amber. Reply ACK only.", on_event
        )
        first_id = session.session_id
        print(f"First response: {first!r}; session: {first_id}")
        if not first_id or "ACK" not in first.upper():
            return 1
        second = await manager.send_stream(key, "What was the secret word?", on_event)
        print(f"Second response: {second!r}; session: {session.session_id}")
        if "amber" not in second.lower() or session.session_id != first_id:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
