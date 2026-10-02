"""Small local Codex app-server client for persisted thread metadata."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tomllib
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from telegram_bot.core.services.cc_events import StreamEvent, _tool_status
from telegram_bot.core.services.codex_mcp import (
    build_codex_mcp_config_args,
    discover_codex_mcp_server_names,
)
from telegram_bot.core.services.providers import CODEX_ADAPTER, codex_process_env

logger = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    """The local Codex app-server could not complete a request."""


class CodexAppServerClient:
    """Run a short-lived app-server connection with the user's Codex state."""

    def __init__(
        self, *, binary: str | None = None, timeout: float = 20.0, cwd: str | Path | None = None
    ) -> None:
        self._binary = binary or CODEX_ADAPTER.safe_binary()
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._cwd = Path(cwd or Path.cwd())
        self._notifications: list[dict[str, Any]] = []

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._proc

    async def __aenter__(self) -> CodexAppServerClient:
        if self._binary is None:
            raise CodexAppServerError("Codex CLI executable is unavailable")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._binary,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=codex_process_env(codex_bin=self._binary),
                cwd=self._cwd,
                limit=8 * 1024 * 1024,
            )
            await self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "telegram_ai_agent",
                        "title": "Telegram AI Agent",
                        "version": "0.1.0",
                    }
                },
            )
            await self._send({"method": "initialized", "params": {}})
        except (OSError, TimeoutError, CodexAppServerError) as exc:
            await self.__aexit__(None, None, None)
            raise CodexAppServerError(f"Codex app-server startup failed: {exc}") from exc
        return self

    async def __aexit__(self, *_args: object) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
            with contextlib.suppress(OSError, BrokenPipeError):
                await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await asyncio.wait_for(proc.stdin.drain(), timeout=self._timeout)
        except (OSError, TimeoutError) as exc:
            raise CodexAppServerError("Codex app-server request could not be sent") from exc

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one RPC; ignore unrelated notifications, never print raw events."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise CodexAppServerError("Codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        await self._send({"id": request_id, "method": method, "params": params})
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._timeout)
            except (TimeoutError, ValueError) as exc:
                raise CodexAppServerError(f"Codex app-server timed out on {method}") from exc
            if not line:
                raise CodexAppServerError(f"Codex app-server closed during {method}")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodexAppServerError("Codex app-server returned invalid JSON") from exc
            if not isinstance(response, dict):
                continue
            if response.get("id") != request_id:
                if "method" in response:
                    self._notifications.append(response)
                continue
            error = response.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", "unknown error"))
                logger.warning("Codex app-server %s failed: %s", method, message)
                raise CodexAppServerError(message)
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError(f"Codex app-server returned no result for {method}")
            return result

    async def wait_for_turn(
        self,
        thread_id: str,
        *,
        timeout: float,
        on_notification: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        """Wait for the completion notification of the first persisted turn."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise CodexAppServerError("Codex app-server is not running")
        async with asyncio.timeout(timeout):
            while True:
                if self._notifications:
                    event = self._notifications.pop(0)
                else:
                    line = await proc.stdout.readline()
                    if not line:
                        raise CodexAppServerError("Codex app-server closed during turn")
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise CodexAppServerError("Codex app-server returned invalid JSON") from exc
                if not isinstance(event, dict):
                    continue
                params = event.get("params")
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    continue
                if on_notification is not None:
                    forwarded = on_notification(event)
                    if asyncio.iscoroutine(forwarded):
                        await forwarded
                if event.get("method") != "turn/completed":
                    continue
                turn = params.get("turn")
                if isinstance(turn, dict):
                    status = turn.get("status")
                    if status not in (None, "completed"):
                        raise CodexAppServerError(f"Codex turn ended with status {status}")
                return


def stream_event_from_notification(event: dict[str, Any]) -> StreamEvent | None:
    """Normalize first-turn app-server progress for the existing Telegram stream UI."""
    method = event.get("method")
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            turn_id = None
        return StreamEvent(
            "turn_start" if method == "turn/started" else "turn_end", "", turn_id=turn_id
        )
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    turn_id = params.get("turnId")
    if not isinstance(turn_id, str):
        turn_id = None
    item_type = item.get("type")
    if method == "item/completed":
        text = item.get("text")
        if (
            item_type == "agentMessage"
            and item.get("phase") == "commentary"
            and isinstance(text, str)
            and text.strip()
        ):
            return StreamEvent("text", text, turn_id=turn_id)
        return None
    if item_type == "commandExecution":
        command = item.get("command")
        return StreamEvent(
            "status",
            _tool_status("Bash", {"command": command} if isinstance(command, str) else None),
            turn_id=turn_id,
        )
    if item_type == "mcpToolCall":
        server, tool = item.get("server"), item.get("tool")
        if isinstance(server, str) and isinstance(tool, str):
            return StreamEvent("status", _tool_status(f"mcp__{server}__{tool}"), turn_id=turn_id)
    if item_type == "fileChange":
        return StreamEvent("status", _tool_status("Edit"), turn_id=turn_id)
    return None


def codex_app_config(cwd: str | Path, mcp_config: str | None) -> dict[str, Any]:
    """Translate the existing safe MCP overrides to app-server config."""
    codex_env = codex_process_env()
    home = Path(codex_env.get("CODEX_HOME", Path.home() / ".codex"))
    inherited = discover_codex_mcp_server_names(cwd, codex_home=home)
    args = build_codex_mcp_config_args(mcp_config, inherited_server_names=inherited)
    config: dict[str, Any] = {}
    for flag, override in zip(args[::2], args[1::2], strict=True):
        if flag != "-c":
            raise CodexAppServerError(f"Unexpected Codex config flag: {flag}")
        key, sep, literal = override.partition("=")
        if not sep or not key.startswith("mcp_servers."):
            raise CodexAppServerError("Invalid Codex MCP override")
        try:
            value = tomllib.loads(f"value = {literal}")["value"]
        except tomllib.TOMLDecodeError as exc:
            raise CodexAppServerError("Invalid Codex MCP value") from exc
        _, name, field = key.split(".", 2)
        servers = config.setdefault("mcp_servers", {})
        servers.setdefault(name, {})[field] = value
    return config


def latest_final_answer(thread: dict[str, Any]) -> str:
    """Extract the final assistant text from a completed persisted turn."""
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        raise CodexAppServerError("Codex thread contains no completed turn")
    latest = turns[-1]
    if not isinstance(latest, dict):
        raise CodexAppServerError("Codex turn is malformed")
    items = latest.get("items")
    if not isinstance(items, list):
        raise CodexAppServerError("Codex turn contains no answer")
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text
    raise CodexAppServerError("Codex turn contains no final answer")


def _thread_list_label(item: dict[str, Any]) -> str | None:
    title = item.get("name")
    if not isinstance(title, str) or not title.strip():
        title = item.get("preview")
        if not isinstance(title, str):
            return None
        if "</telegram-context>" in title:
            title = title.split("</telegram-context>", 1)[1]
    normalized = " ".join(title.split())
    if not normalized or normalized.startswith(("<command-", "<system-reminder>")):
        return None
    return normalized[:120]


def _local_codex_thread_titles() -> dict[str, str]:
    """Read the append-only chat-name index shared with Codex Desktop."""
    codex_home = Path(codex_process_env().get("CODEX_HOME") or Path.home() / ".codex")
    names: dict[str, str] = {}
    try:
        with (codex_home / "session_index.jsonl").open(encoding="utf-8") as index:
            for line in index:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                thread_id = record.get("id")
                title = _thread_list_label({"name": record.get("thread_name")})
                if isinstance(thread_id, str) and title:
                    names[thread_id] = title
    except (OSError, UnicodeError):
        return {}
    return names


async def list_codex_thread_titles(
    *, max_threads: int = 500, fallback_ids: Iterable[str] = ()
) -> dict[str, str]:
    """Prefer local chat names; use app-server for installations without an index."""
    names = await asyncio.to_thread(_local_codex_thread_titles)
    if names:
        return names
    async with CodexAppServerClient() as client:
        for archived in (False, True):
            cursor: str | None = None
            scanned = 0
            while scanned < max_threads:
                params: dict[str, Any] = {
                    "limit": min(100, max_threads - scanned),
                    "sourceKinds": ["cli", "vscode", "exec", "appServer"],
                    "archived": archived,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                result = await client.call("thread/list", params)
                data = result.get("data")
                if not isinstance(data, list) or not data:
                    break
                scanned += len(data)
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    thread_id = item.get("id")
                    title = _thread_list_label(item)
                    if isinstance(thread_id, str) and title:
                        names[thread_id] = title
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        # Some older local rollouts are readable by ID but omitted from
        # thread/list. Fetch only their metadata; includeTurns=False avoids
        # loading large histories just to label /resume buttons.
        for thread_id in dict.fromkeys(fallback_ids):
            if thread_id in names:
                continue
            try:
                result = await client.call(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                )
            except CodexAppServerError:
                continue
            thread = result.get("thread")
            if isinstance(thread, dict) and (title := _thread_list_label(thread)):
                names[thread_id] = title
    return names


async def get_thread_goal(thread_id: str) -> dict[str, Any] | None:
    async with CodexAppServerClient() as client:
        result = await client.call("thread/goal/get", {"threadId": thread_id})
    goal = result.get("goal")
    return goal if isinstance(goal, dict) else None


async def set_thread_goal(
    thread_id: str,
    *,
    objective: str | None = None,
    status: str | None = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"threadId": thread_id}
    if objective is not None:
        objective = objective.strip()
        if not objective or len(objective) > 4000:
            raise ValueError("Goal objective must contain 1-4000 characters")
        params["objective"] = objective
    if status is not None:
        if status not in {"active", "paused", "blocked", "complete"}:
            raise ValueError("Unsupported goal status")
        params["status"] = status
    if token_budget is not None:
        if token_budget <= 0:
            raise ValueError("Goal token budget must be positive")
        params["tokenBudget"] = token_budget
    async with CodexAppServerClient() as client:
        result = await client.call("thread/goal/set", params)
    goal = result.get("goal")
    if not isinstance(goal, dict):
        raise CodexAppServerError("Codex did not return the updated goal")
    return goal


async def clear_thread_goal(thread_id: str) -> bool:
    async with CodexAppServerClient() as client:
        result = await client.call("thread/goal/clear", {"threadId": thread_id})
    return bool(result.get("cleared"))
