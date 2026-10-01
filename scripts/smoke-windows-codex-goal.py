"""Exercise goal set/get/clear on a disposable Codex thread."""

from __future__ import annotations

import asyncio
import sys

from telegram_bot.core.services.codex_app_server import (
    clear_thread_goal,
    get_thread_goal,
    set_thread_goal,
)


async def main(thread_id: str) -> None:
    original = await get_thread_goal(thread_id)
    if original is not None:
        raise SystemExit("Refusing to overwrite an existing goal")
    try:
        set_result = await set_thread_goal(
            thread_id, objective="Telegram goal smoke", status="active", token_budget=1000
        )
        observed = await get_thread_goal(thread_id)
        print("set:", set_result.get("objective"), set_result.get("status"))
        print("get:", observed.get("objective") if observed else None)
        if (
            observed is None
            or observed.get("objective") != "Telegram goal smoke"
            or observed.get("tokenBudget") != 1000
        ):
            raise SystemExit(1)
        for status in ("paused", "active", "complete"):
            changed = await set_thread_goal(thread_id, status=status)
            reread = await get_thread_goal(thread_id)
            if changed.get("status") != status or reread is None or reread.get("status") != status:
                raise SystemExit(f"Goal status did not become {status}")
            print("status:", status)
    finally:
        print("cleared:", await clear_thread_goal(thread_id))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke-windows-codex-goal.py THREAD_ID")
    asyncio.run(main(sys.argv[1]))
