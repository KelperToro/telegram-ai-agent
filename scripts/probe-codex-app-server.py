"""Read-only probe of the installed Codex app-server protocol on Windows."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from telegram_bot.core.services.providers import CODEX_ADAPTER


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        CODEX_ADAPTER.binary(),
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[1],
        limit=8 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def call(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = {"id": request_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
            if not raw:
                raise RuntimeError("Codex app-server closed stdout")
            response = json.loads(raw)
            if response.get("id") == request_id:
                return response

    try:
        initialized = await call(
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram_ai_agent_probe",
                    "title": "Telegram AI Agent Probe",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if "error" in initialized:
            raise RuntimeError(str(initialized["error"]))
        proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        await proc.stdin.drain()
        listed = await call(
            2,
            "thread/list",
            {
                "limit": 100,
                "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
            },
        )
        if "error" in listed:
            raise RuntimeError(str(listed["error"]))
        threads = listed.get("result", {}).get("data", [])
        print("thread_count:", len(threads))
        print("sources:", dict(Counter(str(item.get("source")) for item in threads)))
        print("more_pages:", listed.get("result", {}).get("nextCursor") is not None)
        if threads:
            goal = await call(3, "thread/goal/get", {"threadId": threads[0]["id"]})
            print("goal_rpc:", "ok" if "result" in goal else "error")
        if len(sys.argv) > 1 and sys.argv[1] == "--create":
            created = await call(
                5,
                "thread/start",
                {
                    "cwd": str(Path(__file__).resolve().parents[1]),
                    "threadSource": "telegram_ai_agent",
                },
            )
            thread_id = created.get("result", {}).get("thread", {}).get("id")
            if not isinstance(thread_id, str):
                raise RuntimeError(str(created.get("error", "thread/start failed")))
            await call(
                6,
                "thread/name/set",
                {"threadId": thread_id, "name": "Telegram bridge visibility test"},
            )
            print("created_thread:", thread_id)
            started = await call(
                7,
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "Reply exactly SOURCEAPP"}],
                },
            )
            if "error" in started:
                raise RuntimeError(str(started["error"]))
            while True:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
                if not raw:
                    raise RuntimeError("app-server closed before turn completion")
                event = json.loads(raw)
                if event.get("method") == "turn/completed":
                    print("turn_completed:", event.get("params", {}).get("threadId") == thread_id)
                    break
        elif len(sys.argv) > 2 and sys.argv[1] == "--resume":
            resumed = await call(5, "thread/resume", {"threadId": sys.argv[2]})
            print("resume_ok:", "result" in resumed)
            if "error" in resumed:
                print("resume_error:", resumed["error"].get("message", "unknown"))
        elif len(sys.argv) > 1:
            read = await call(4, "thread/read", {"threadId": sys.argv[1]})
            thread = read.get("result", {}).get("thread", {})
            print(
                "target:",
                {
                    "found": bool(thread),
                    "source": thread.get("source"),
                    "has_name": bool(thread.get("name")),
                    "status": thread.get("status"),
                },
            )
            print("thread_keys:", list(thread))
    finally:
        proc.stdin.close()
        await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            proc.kill()
            await proc.wait()


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Windows only")
    asyncio.run(main())
