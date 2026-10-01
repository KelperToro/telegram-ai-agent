"""Codex CLI update service for bot-owned update flows."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from telegram_bot.core.services.providers import CODEX_ADAPTER, codex_process_env

logger = logging.getLogger(__name__)

CodexUpdateStatus = Literal[
    "success",
    "failed",
    "timeout",
    "already_running",
    "blocked_active_sessions",
    "skipped_cooldown",
    "disabled",
]

ActiveCheck = Callable[[], bool | Awaitable[bool]]

_OUTPUT_LIMIT = 4000
_REDACTION_PATTERNS = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(token|api[_-]?key|secret|password|authorization)(\s*[=:]\s*)(\S+)"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(session[_-]?string)(\s*[=:]\s*)(\S+)"), r"\1\2[REDACTED]"),
)


@dataclass(frozen=True)
class CodexUpdateState:
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    last_status: str | None = None
    last_output: str = ""


@dataclass(frozen=True)
class CodexUpdateResult:
    status: CodexUpdateStatus
    output: str = ""
    returncode: int | None = None


class CodexUpdateService:
    """Run ``codex update`` with bot-safe locking, env, timeout, and cooldown."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        timeout_sec: float = 180.0,
        cooldown_sec: float = 24 * 3600,
        enabled: bool = True,
    ) -> None:
        self._state_path = Path(state_path)
        self._timeout_sec = timeout_sec
        self._cooldown_sec = cooldown_sec
        self._enabled = enabled
        self._lock = asyncio.Lock()

    def status(self) -> CodexUpdateState:
        return self._load_state()

    async def run_manual(self, *, active_check: ActiveCheck) -> CodexUpdateResult:
        return await self._run(active_check=active_check, respect_cooldown=False)

    async def run_auto(self, *, active_check: ActiveCheck) -> CodexUpdateResult:
        return await self._run(active_check=active_check, respect_cooldown=True)

    async def _run(
        self,
        *,
        active_check: ActiveCheck,
        respect_cooldown: bool,
    ) -> CodexUpdateResult:
        if not self._enabled:
            return CodexUpdateResult("disabled")
        if self._lock.locked():
            return CodexUpdateResult("already_running")
        if await self._active(active_check):
            return CodexUpdateResult("blocked_active_sessions")
        if respect_cooldown and self._inside_cooldown():
            return CodexUpdateResult("skipped_cooldown")

        async with self._lock:
            if await self._active(active_check):
                return CodexUpdateResult("blocked_active_sessions")
            if respect_cooldown and self._inside_cooldown():
                return CodexUpdateResult("skipped_cooldown")
            return await self._run_update_process()

    async def _active(self, active_check: ActiveCheck) -> bool:
        result = active_check()
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    def _inside_cooldown(self) -> bool:
        state = self._load_state()
        if state.last_success_at is None:
            return False
        return (time.time() - state.last_success_at) < self._cooldown_sec

    async def _run_update_process(self) -> CodexUpdateResult:
        try:
            binary = CODEX_ADAPTER.safe_binary()
        except RuntimeError as exc:
            result = CodexUpdateResult("failed", output=str(exc)[:_OUTPUT_LIMIT])
            self._save_state(time.time(), result)
            return result
        if binary is None:
            result = CodexUpdateResult("failed", output="Unsafe or missing Codex binary")
            self._save_state(time.time(), result)
            return result
        argv = [binary, "update"]
        logger.info("Starting Codex update: argv=%s", argv)
        started_at = time.time()
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                shell=False,
                env=codex_process_env(codex_bin=binary),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_sec,
            )
        except TimeoutError:
            if process is not None:
                await self._kill_process_group(process)
            result = CodexUpdateResult("timeout", output="Codex update timed out")
            self._save_state(started_at, result)
            return result
        except Exception as exc:
            logger.warning("Codex update failed to start", exc_info=True)
            result = CodexUpdateResult("failed", output=str(exc)[:_OUTPUT_LIMIT])
            self._save_state(started_at, result)
            return result

        output = self._redact(self._format_output(stdout, stderr))
        if process.returncode == 0:
            result = CodexUpdateResult("success", output=output, returncode=process.returncode)
        else:
            result = CodexUpdateResult("failed", output=output, returncode=process.returncode)
        self._save_state(started_at, result)
        return result

    @staticmethod
    async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=5)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            getattr(os, "killpg")(  # noqa: B009
                getattr(os, "getpgid")(process.pid),  # noqa: B009
                getattr(signal, "SIGKILL"),  # noqa: B009
            )
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=5)

    @staticmethod
    def _format_output(stdout: bytes, stderr: bytes) -> str:
        text = "\n".join(
            part.decode(errors="replace").strip() for part in (stdout, stderr) if part
        ).strip()
        if len(text) > _OUTPUT_LIMIT:
            return text[-_OUTPUT_LIMIT:]
        return text

    @staticmethod
    def _redact(text: str) -> str:
        redacted = text
        for pattern, replacement in _REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted

    def _load_state(self) -> CodexUpdateState:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return CodexUpdateState()
        if not isinstance(raw, dict):
            return CodexUpdateState()
        last_output = raw.get("last_output")
        return CodexUpdateState(
            last_attempt_at=raw.get("last_attempt_at")
            if isinstance(raw.get("last_attempt_at"), int | float)
            else None,
            last_success_at=raw.get("last_success_at")
            if isinstance(raw.get("last_success_at"), int | float)
            else None,
            last_status=raw.get("last_status") if isinstance(raw.get("last_status"), str) else None,
            last_output=last_output if isinstance(last_output, str) else "",
        )

    def _save_state(self, attempted_at: float, result: CodexUpdateResult) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._load_state()
        data = {
            "last_attempt_at": attempted_at,
            "last_success_at": (
                attempted_at if result.status == "success" else previous.last_success_at
            ),
            "last_status": result.status,
            "last_output": self._redact(result.output),
        }
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)
