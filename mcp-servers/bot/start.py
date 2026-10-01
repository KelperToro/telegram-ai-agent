"""Windows-compatible launcher for the bundled Telegram MCP server."""

from __future__ import annotations

import os
import runpy
from pathlib import Path

from telegram_bot.core.env_file import read_exact_env_file


def main() -> None:
    root = Path(
        os.environ.get("APP_ROOT")
        or os.environ.get("PROJECT_DIR")
        or Path(__file__).resolve().parents[2]
    )
    env_file = Path(os.environ.get("ENV_FILE") or root / ".env")
    if not os.environ.get("BOT_TOKEN"):
        try:
            token = read_exact_env_file(env_file).get("TELEGRAM_BOT_TOKEN", "")
        except OSError:
            token = ""
        if not token:
            raise SystemExit(f"TELEGRAM_BOT_TOKEN not set in {env_file}")
        os.environ["BOT_TOKEN"] = token
    runpy.run_path(str(Path(__file__).with_name("server.py")), run_name="__main__")


if __name__ == "__main__":
    main()
